#!/usr/bin/env python3
"""
CRM (Baserow) -> Kumu export pipeline, UK TRE Community schema (2026-10-05).

Reads four Baserow exports (People-email, People-stacked, Organisations,
Working-Groups), builds Kumu-ready elements + connections workbooks, and
writes TWO versions:

  kumu-import-internal-<date>.xlsx  Every person except consent "none", as
                                    their best-guess People-stacked node,
                                    labelled by Baserow UUID, with the same
                                    hashed pseudonymised-id as the public map.
                                    No names. Full profile, per-email
                                    Slack/Jisc detail.
  kumu-import-public-<date>.xlsx    Consent-filtered. Only people whose
                                    (rolled-up) consent is public appear.
                                    Everyone is labelled by a stable hashed
                                    pseudonym, except public-identifiable
                                    people with a complete first name +
                                    surname in People-email (or, as a
                                    fallback, the optional names file), who
                                    are labelled by name. No sharepoint-ids,
                                    no UUIDs.

Deterministic, pure Python (pandas + openpyxl). No LLM calls.

Run from the repo root:

    python community-mapping/code/crm-kumu-visualisation/scripts/pipeline.py \
        --input-dir private/crm/baserow-export/2026-10-05

Outputs are written to the input directory unless --output-dir is given.
The pseudonymisation salt lives at a FIXED path (--salt-file, default
private/crm/pseudonym-salt.txt) so pseudonyms stay stable across dated
export folders.

  references/kumu-display-settings-internal
                                    GENERATED each run from the hand-edited
                                    references/kumu-display-settings-internal.template:
                                    the Kumu legend/colour code listing only
                                    the type-display values present in the
                                    internal elements (colours from
                                    config.TYPE_DISPLAY_COLOURS).
  references/kumu-display-settings-public
                                    The same, for the PUBLIC map, generated from
                                    references/kumu-display-settings-public.template
                                    (legend limited to the types in the public
                                    elements; text blocks and public-only filters).

The pipeline STOPS (non-zero exit, nothing written) on:
  - missing expected columns (schema drift),
  - commas in organisation / working-group names (flag-comma-error is set),
  - any People-email row affiliated with the "**Other" placeholder
    organisation / working group, and any comma in a first name / surname that
    would be used as a public label (all reported together with the comma
    rows, in one message, BEFORE the pseudonymisation salt is read),
  - duplicate sharepoint-ids, or an email linked to more than one person,
  - positional lookup lists on People-stacked whose lengths disagree with
    the linked emails (the position-based Slack/Jisc design relies on this),
  - unreadable boolean cells.
Softer problems are printed (and written to a report file) as warnings.

See references/schema.md and references/how-to-run.md.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import secrets
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import config


# Resolved from this file, not the current directory, so the run works from
# the repo root (or anywhere).
REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"
DISPLAY_SETTINGS_NAME = "kumu-display-settings-internal"
DISPLAY_SETTINGS_PUBLIC_NAME = "kumu-display-settings-public"
PIS_URL_PLACEHOLDER = "[PIS-URL]"
TYPE_COLOURS_PLACEHOLDER = "{{TYPE_COLOURS}}"


class PipelineError(Exception):
    """Raised for conditions that must stop the run."""


class PreflightStop(PipelineError):
    """Rows that must be fixed in Baserow. The message is already complete
    (one or more 'STOPPED: ...' sections), so main() prints it as is."""


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------

def find_input_file(input_dir: str, stub: str) -> str:
    matches = []
    for ext in ("xlsx", "csv"):
        matches += glob.glob(os.path.join(input_dir, f"baserow-export-{stub}-*.{ext}"))
        matches += glob.glob(os.path.join(input_dir, f"baserow-export-{stub}.{ext}"))
    # Exclude Excel lock files (~$...), created while a file is open.
    matches = sorted(m for m in set(matches) if not os.path.basename(m).startswith("~$"))
    if not matches:
        raise PipelineError(
            f"No input file found for '{stub}' in {input_dir}. Expected a file "
            f"named like 'baserow-export-{stub}-YYYYMMDD.xlsx' (or .csv)."
        )
    if len(matches) > 1:
        print(
            f"  note: multiple files matched '{stub}' "
            f"({[os.path.basename(m) for m in matches]}); using the last "
            f"(assumed most recent by filename): {os.path.basename(matches[-1])}"
        )
    return matches[-1]


def load_table(input_dir: str, stub: str) -> pd.DataFrame:
    path = find_input_file(input_dir, stub)
    try:
        if path.lower().endswith(".csv"):
            df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        else:
            df = pd.read_excel(path, sheet_name=0, dtype=str)
    except Exception as e:  # noqa: BLE001 - re-raised with context
        raise PipelineError(
            f"Could not read {os.path.basename(path)} ({e}). If the file is "
            f"open in Excel/Numbers, close it and re-run."
        ) from e
    df.columns = [str(c).strip() for c in df.columns]
    expected = set(config.EXPECTED_COLUMNS[stub])
    actual = set(df.columns)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise PipelineError(
            f"Schema drift in {os.path.basename(path)}: missing expected "
            f"column(s) {sorted(missing)}. Re-export the structure, update "
            f"config.EXPECTED_COLUMNS, and check the field maps that use them."
        )
    if extra:
        print(
            f"  note: {os.path.basename(path)} has column(s) not registered in "
            f"config.py: {sorted(extra)} -- ignored."
        )
    return df


def safe_str(value) -> str:
    """str(value).strip(), treating pandas NaN/None as empty (NaN is truthy)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def split_multi(value) -> list[str]:
    """Baserow multi-value cells export as comma-separated text.
    Safe for org / working-group names because commas are forbidden in them."""
    value = safe_str(value)
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_multi_select(value, options: list[str], where: str, warnings: set) -> list[str]:
    """
    Parse a Baserow multiple-select cell. Options may contain commas, so the
    text is matched against the known options (longest first) rather than split
    on commas. Anything that matches no option is kept as typed (up to the next
    comma) with a warning. Returns the distinct values: known options in
    config order, then unknown values in the order found.
    """
    # Baserow CSV wraps a comma-containing option in double quotes: drop them
    # (no option contains a quote).
    text = safe_str(value).replace('"', "")
    if not text:
        return []
    by_length = sorted(options, key=len, reverse=True)
    found: list[str] = []
    pos = 0
    while pos < len(text):
        rest = text[pos:]
        for opt in by_length:
            if rest.startswith(opt) and (len(rest) == len(opt) or rest[len(opt):].lstrip().startswith(",")):
                found.append(opt)
                pos += len(opt)
                break
        else:
            comma = rest.find(",")
            token = (rest if comma < 0 else rest[:comma]).strip()
            if token:
                warnings.add(
                    f"{where}: value '{token}' is not in the known options for this field "
                    f"(config.MULTI_SELECT_FIELDS) -- kept as typed. Add it to the list in "
                    f"config.py (a value containing a comma is otherwise split into fragments)."
                )
                found.append(token)
            pos += len(rest) if comma < 0 else comma
        while pos < len(text) and text[pos] in ", ":
            pos += 1
    return order_options(found, options)


def order_options(values, options: list[str]) -> list[str]:
    """Distinct values: known options in config order, then unknown in first-seen order."""
    distinct = unique(values)
    known = [o for o in options if o in distinct]
    return known + [v for v in distinct if v not in options]


def join_pipe(values) -> str:
    return "|".join(values)


def unique(seq):
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


_TRUE = {"true", "yes", "1", "y", "checked", "tick", "✓", "✔"}
_FALSE = {"false", "no", "0", "n", "unchecked", "cross", "✗", "✘"}


def parse_bool(token: str, where: str) -> bool:
    t = token.strip().lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    raise PipelineError(f"Cannot interpret '{token}' as true/false ({where}).")


def parse_bool_cell(value, where: str) -> bool:
    """A single boolean cell; blank means False."""
    v = safe_str(value)
    return parse_bool(v, where) if v else False


def parse_bool_list(value, where: str) -> list[bool]:
    v = safe_str(value)
    if not v:
        return []
    return [parse_bool(t, where) for t in v.split(",")]


def yes_no(b: bool) -> str:
    return "yes" if b else "no"


# ---------------------------------------------------------------------------
# Pseudonymisation (stable across runs, not derivable without the salt file)
# ---------------------------------------------------------------------------

def load_or_create_salt(salt_path: str) -> str:
    if os.path.exists(salt_path):
        with open(salt_path, "r") as f:
            return f.read().strip()
    salt = secrets.token_hex(16)
    os.makedirs(os.path.dirname(os.path.abspath(salt_path)), exist_ok=True)
    with open(salt_path, "w") as f:
        f.write(salt)
    print(f"  generated new pseudonymisation salt at {salt_path} (keep private and stable)")
    return salt


def pseudonymise(key: str, salt: str) -> str:
    digest = hashlib.sha256((salt + key).encode("utf-8")).hexdigest()[:8]
    return f"Person-{digest}"


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------

def consent_rank(value: str, where: str, warnings: set) -> int:
    v = safe_str(value).lower()
    if not v:
        return config.CONSENT_DEFAULT_RANK
    if v in config.CONSENT_RANK:
        return config.CONSENT_RANK[v]
    warnings.add(
        f"{where}: unrecognised consent-mapping value '{value}' -- treated as "
        f"'none' (most restrictive)."
    )
    return 0


# ---------------------------------------------------------------------------
# Pre-flight checks (hard stops)
# ---------------------------------------------------------------------------

def _label_field_for(stub: str) -> str | None:
    return {
        config.STUB_ORGANISATIONS: config.ORG_NODE["label_field"],
        config.STUB_WORKING_GROUPS: config.WG_NODE["label_field"],
    }.get(stub)


def check_comma_flags(tables: dict[str, pd.DataFrame]) -> list[str]:
    """
    Rows whose name contains a comma. A row is a problem if the Baserow flag
    column (config.COMMA_FLAG_FIELD) is non-empty after stripping whitespace
    (NaN counts as empty), or if a name column listed in
    config.COMMA_FREE_FIELDS contains a comma. Returns message lines
    (empty list = fine); never raises.
    """
    lines = []
    for stub, fields in config.COMMA_FREE_FIELDS.items():
        df = tables[stub]
        label_field = _label_field_for(stub)
        for _, row in df.iterrows():
            flagged = bool(safe_str(row[config.COMMA_FLAG_FIELD]))
            comma_fields = [f for f in fields if "," in safe_str(row[f])]
            if not (flagged or comma_fields):
                continue
            line = f"  - {stub} id={safe_str(row['id'])}: '{safe_str(row[label_field])}'"
            if not flagged:
                line += (f" (the comma is in {comma_fields}, which '{config.COMMA_FLAG_FIELD}' "
                         f"does not cover)")
            lines.append(line)
    if not lines:
        return []
    return [
        f"STOPPED: {len(lines)} row(s) have a comma in their name "
        f"('{config.COMMA_FLAG_FIELD}' is set in Baserow):",
        *lines,
        "Remove the comma(s) in Baserow, re-export the CSV(s), and re-run.",
    ]


def check_other_placeholder(tables: dict[str, pd.DataFrame]) -> list[str]:
    """
    People-email rows affiliated with the exact placeholder
    config.OTHER_PLACEHOLDER ("**Other") in any of config.OTHER_CHECK_FIELDS.
    One line per People-email row, naming the columns. Returns message lines
    (empty list = fine); never raises.
    """
    lines = []
    for _, row in tables[config.STUB_PEOPLE_EMAIL].iterrows():
        hits = [
            f for f in config.OTHER_CHECK_FIELDS
            if config.OTHER_PLACEHOLDER in split_multi(row[f])
        ]
        if hits:
            lines.append(
                f"  - {config.STUB_PEOPLE_EMAIL} id={safe_str(row['id'])} "
                f"(sharepoint-id {safe_str(row['sharepoint-id-email'])}): {', '.join(hits)}"
            )
    if not lines:
        return []
    return [
        f"STOPPED: {len(lines)} row(s) are affiliated with '{config.OTHER_PLACEHOLDER}' "
        f"(an organisation or working group that is not yet in the CRM):",
        *lines,
        f"Replace '{config.OTHER_PLACEHOLDER}' in Baserow with the real organisation or working "
        f"group (add it to Organisations / Working-Groups first if needed), re-export the "
        f"CSV(s), and re-run.",
    ]


def resolve_name(cluster, where, warnings):
    """
    Public-identifiable label from the user-nominated first name and surname:
    for each part the first non-empty value in sharepoint-id order (a warning
    if the cluster's emails disagree). BOTH parts are required.
    Returns (label, sources) with sources = [(Email, field, value), ...],
    or (None, []) when either part is missing.
    """
    picked = []
    for field, attr in zip(config.NAME_FIELDS, ("name_first", "name_surname")):
        vals = [(e, getattr(e, attr)) for e in cluster if getattr(e, attr)]
        if not vals:
            return None, []
        if warnings is not None and len({v for _, v in vals}) > 1:
            warnings.add(
                f"{where}: emails give DIFFERENT values for '{field}' "
                f"({'; '.join(f'{e.sid}: {v}' for e, v in vals)}) -- first used."
            )
        picked.append((vals[0][0], field, vals[0][1]))
    return f"{picked[0][2]} {picked[1][2]}", picked


def check_name_commas(tables: dict[str, pd.DataFrame]) -> list[str]:
    """
    Commas in a first name / surname that WOULD be used as a public label
    (public-identifiable clusters with both parts). Needs the people grouping,
    so it builds it with throwaway warnings; if the data is too broken to group
    it returns nothing and the normal run reports that error.
    """
    try:
        emails = load_emails(tables[config.STUB_PEOPLE_EMAIL], set())
        groups = build_groups(tables[config.STUB_PEOPLE_STACKED], emails, set())
    except PipelineError:
        return []
    lines, seen = [], set()
    for key, members in groups:
        clusters, _status = build_clusters(members)
        for cluster in clusters:
            if min(e.rank for e in cluster) < config.CONSENT_PUBLIC_IDENTIFIABLE_RANK:
                continue
            _label, sources = resolve_name(cluster, f"person {key}", None)
            for e, field, value in sources:
                if "," in value and (e.row_id, field) not in seen:
                    seen.add((e.row_id, field))
                    lines.append(
                        f"  - {config.STUB_PEOPLE_EMAIL} id={e.row_id} "
                        f"(sharepoint-id {e.sid}): '{field}' = '{value}'"
                    )
    if not lines:
        return []
    return [
        f"STOPPED: {len(lines)} row(s) have a comma in a first name or surname that would be "
        f"shown on the public map:",
        *lines,
        "Remove the comma(s) in Baserow, re-export the CSV(s), and re-run.",
    ]


def preflight(tables: dict[str, pd.DataFrame]) -> None:
    """Run every hard-stop data check and report ALL problems in one message,
    before the salt is read or anything is built or written."""
    sections = [
        "\n".join(lines)
        for lines in (check_comma_flags(tables), check_other_placeholder(tables),
                      check_name_commas(tables))
        if lines
    ]
    if sections:
        raise PreflightStop("\n\n".join(sections))


class Email:
    """One People-email row."""

    def __init__(self, row: pd.Series, warnings: set):
        self.sid = safe_str(row["sharepoint-id-email"])
        self.row_id = safe_str(row["id"])
        self.uuid = safe_str(row[config.EMAIL_UUID_FIELD]).lower()
        self.nominated = safe_str(row[config.EMAIL_UUID_NOMINATED_FIELD]).lower()
        stacked = split_multi(row["People-stacked"])
        if len(stacked) > 1:
            raise PipelineError(
                f"Email sharepoint-id {self.sid} is linked to more than one "
                f"People-stacked person ({stacked}). Fix in Baserow."
            )
        self.stacked_uuid = stacked[0].lower() if stacked else ""
        where = f"sharepoint-id {self.sid}"
        self.rank = consent_rank(row["consent-mapping"], where, warnings)
        self.slack = parse_bool_cell(row["slack"], f"People-email.slack, {where}")
        self.jisc = parse_bool_cell(row["jisc"], f"People-email.jisc, {where}")
        self.validated = parse_bool_cell(
            row["data-user-validation"], f"People-email.data-user-validation, {where}"
        )
        self.bounced = parse_bool_cell(
            row["email-bounced"], f"People-email.email-bounced, {where}"
        )
        placeholder = config.PERSONAL_ORG_PLACEHOLDER.strip().lower()
        self.orgs = [
            o for o in split_multi(row["email-organisation"]) if o.strip().lower() != placeholder
        ]
        self.wg_lead = split_multi(row["email-wg-lead"])
        self.wg_member = split_multi(row["email-wg-member"])
        self.fields = {}
        for kumu_name, (src, _strategy) in config.PERSON_FIELD_MAP.items():
            if src in config.MULTI_SELECT_FIELDS:
                self.fields[kumu_name] = join_pipe(parse_multi_select(
                    row[src], config.MULTI_SELECT_FIELDS[src],
                    f"People-email {src}, {where}", warnings))
            else:
                self.fields[kumu_name] = safe_str(row[src])
        # User-nominated first name / surname (public identifiable label only).
        self.name_first = safe_str(row[config.NAME_FIELDS[0]])
        self.name_surname = safe_str(row[config.NAME_FIELDS[1]])

    def sort_key(self):
        try:
            return (0, int(self.sid))
        except ValueError:
            return (1, self.sid)


def load_emails(df: pd.DataFrame, warnings: set) -> dict[str, Email]:
    emails: dict[str, Email] = {}
    for _, row in df.iterrows():
        e = Email(row, warnings)
        if not e.sid:
            raise PipelineError("A People-email row has a blank sharepoint-id-email.")
        if e.sid in emails:
            raise PipelineError(f"Duplicate sharepoint-id-email {e.sid} in People-email.")
        emails[e.sid] = e
    return emails


def check_positional_lists(stacked: pd.DataFrame, emails: dict[str, Email], warnings: set) -> None:
    """
    The per-email Slack/Jisc design reads position-aligned lookup lists on
    People-stacked. Safeguard: every aligned list must have exactly one entry
    per linked email, or the run stops. Where lengths agree, positions are
    also compared against the exact join on sharepoint-id; a disagreement
    means list order no longer matches link order (warned; the exact join
    from People-email is what the pipeline then uses).
    """
    problems = []
    for _, row in stacked.iterrows():
        uuid = safe_str(row["baserow-UUID-person"])
        ids = split_multi(row[config.STACKED_ID_LIST_FIELD])
        count_raw = safe_str(row[config.STACKED_COUNT_FIELD])
        if count_raw and count_raw.split(".")[0] != str(len(ids)):
            problems.append(
                f"{uuid}: email-count={count_raw} but {len(ids)} sharepoint-id(s) listed ({ids})"
            )
        for sid in ids:
            if sid not in emails:
                problems.append(f"{uuid}: linked sharepoint-id {sid} is not in the People-email export")
        parsed = {}
        for stacked_field, email_attr in config.STACKED_BOOLEAN_LIST_FIELDS.items():
            raw = safe_str(row[stacked_field])
            vals = parse_bool_list(raw, f"People-stacked.{stacked_field}, person {uuid}")
            if len(vals) != len(ids):
                problems.append(
                    f"{uuid}: '{stacked_field}' has {len(vals)} value(s) for {len(ids)} "
                    f"linked email(s) (cell: '{raw}', ids: {ids})"
                )
            parsed[stacked_field] = (vals, email_attr)
        for stacked_field, (vals, email_attr) in parsed.items():
            if len(vals) == len(ids):
                for sid, v in zip(ids, vals):
                    if sid in emails and getattr(emails[sid], email_attr) != v:
                        warnings.add(
                            f"{uuid}: position of sharepoint-id {sid} in '{stacked_field}' "
                            f"disagrees with People-email. List order on People-stacked is "
                            f"unreliable; the exact People-email values were used."
                        )
    if problems:
        raise PipelineError(
            "Positional lookup lists on People-stacked are not aligned with the "
            "linked emails. Fix in Baserow (blank/missing values in a lookup, or "
            "a link not yet saved?) and re-export:\n  - " + "\n  - ".join(problems)
        )


# ---------------------------------------------------------------------------
# People grouping
# ---------------------------------------------------------------------------

def build_groups(stacked: pd.DataFrame, emails: dict[str, Email], warnings: set):
    """
    Returns list of (person_key, [Email, ...]) in stable order.
    person_key is the People-stacked UUID, or "email-<uuid>" for an email not
    linked to any stacked person.
    Cross-checks the stacked link list against the People-email link column.
    """
    stacked_uuids = {}
    for _, row in stacked.iterrows():
        u = safe_str(row["baserow-UUID-person"]).lower()
        if not u:
            raise PipelineError("A People-stacked row has a blank baserow-UUID-person.")
        if u in stacked_uuids:
            raise PipelineError(f"Duplicate baserow-UUID-person {u}.")
        stacked_uuids[u] = set(split_multi(row[config.STACKED_ID_LIST_FIELD]))

    groups: dict[str, list[Email]] = {u: [] for u in stacked_uuids}
    for e in emails.values():
        if e.stacked_uuid and e.stacked_uuid in groups:
            groups[e.stacked_uuid].append(e)
        elif e.stacked_uuid:
            warnings.add(
                f"sharepoint-id {e.sid} links to People-stacked '{e.stacked_uuid}', "
                f"which is not in the People-stacked export."
            )
            groups[f"email-{e.uuid or e.sid}"] = [e]
        else:
            warnings.add(
                f"sharepoint-id {e.sid} is not linked to any People-stacked person; "
                f"shown as its own node."
            )
            groups[f"email-{e.uuid or e.sid}"] = [e]

    for u, sids in stacked_uuids.items():
        actual = {e.sid for e in groups[u]}
        if sids != actual:
            warnings.add(
                f"{u}: People-stacked lists sharepoint-ids {sorted(sids)} but People-email "
                f"links {sorted(actual)}."
            )
    out = []
    for key in sorted(groups):
        members = sorted(groups[key], key=Email.sort_key)
        if not members:
            warnings.add(f"People-stacked person {key} has no linked emails; skipped.")
            continue
        out.append((key, members))
    return out


def build_clusters(members: list[Email]) -> tuple[list[list[Email]], str]:
    """
    Split one stacked person's emails into clusters of CONFIRMED-linked
    emails (union-find over pairs where either email's user-nominated UUID
    field contains the other's UUID). Returns (clusters, link_status).
    link_status: single | confirmed | partial | unconfirmed.
    """
    parent = {e.sid: e.sid for e in members}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in members:
        for b in members:
            if a is b:
                continue
            if (b.uuid and b.uuid in a.nominated) or (a.uuid and a.uuid in b.nominated):
                parent[find(a.sid)] = find(b.sid)

    buckets: dict[str, list[Email]] = {}
    for e in members:
        buckets.setdefault(find(e.sid), []).append(e)
    clusters = sorted(
        (sorted(c, key=Email.sort_key) for c in buckets.values()),
        key=lambda c: Email.sort_key(c[0]),
    )
    if len(members) == 1:
        status = "single"
    elif len(clusters) == 1:
        status = "confirmed"
    elif len(clusters) == len(members):
        status = "unconfirmed"
    else:
        status = "partial"
    return clusters, status


def person_consent_rank(all_members: list[Email]) -> int:
    """
    Consent for a STACKED person, taken over ALL their emails (bounced ones too):
      - "none" on any email blocks the whole stacked person;
      - otherwise an email that has not been asked says nothing, and consent given
        on any email covers all of them (an address or UUID may have been lost or
        bounced); if different levels were given, the most restrictive of those given;
      - if no email has given consent, not-asked.
    """
    ranks = [e.rank for e in all_members]
    if any(r == config.CONSENT_NONE_RANK for r in ranks):
        return config.CONSENT_NONE_RANK
    given = [r for r in ranks if r > config.CONSENT_DEFAULT_RANK]
    return min(given) if given else config.CONSENT_DEFAULT_RANK


def aggregate(members: list[Email], where: str, warnings: set, rank: int | None = None) -> dict:
    """Combine one person's (or cluster's) emails into a single record.
    rank: the stacked person's consent (person_consent_rank); defaults to the
    most restrictive of these emails."""
    if rank is None:
        rank = min(e.rank for e in members)
    agg = {
        "emails": members,
        "rank": rank,
        "consent": config.CONSENT_CANONICAL[rank],
        "fields": {},
        # (edge_type, target label) -> [sids]
        "edges": {},
    }
    for kumu_name, (src, strategy) in config.PERSON_FIELD_MAP.items():
        vals = [(e.sid, e.fields[kumu_name]) for e in members if e.fields[kumu_name]]
        if strategy == "first":
            agg["fields"][kumu_name] = vals[0][1] if vals else ""
            if len({v for _, v in vals}) > 1:
                warnings.add(
                    f"{where}: emails give DIFFERENT values for '{kumu_name}' "
                    f"({'; '.join(f'{s}: {v}' for s, v in vals)}) -- first used."
                )
        elif strategy == "union-options":
            tokens = [t for _, v in vals for t in v.split("|") if t]
            agg["fields"][kumu_name] = join_pipe(
                order_options(tokens, config.MULTI_SELECT_FIELDS[src]))
        else:
            agg["fields"][kumu_name] = join_pipe(unique(v for _, v in vals))
    for e in members:
        for label in e.orgs:
            agg["edges"].setdefault(("affiliation", label), []).append(e.sid)
        for label in e.wg_lead:
            agg["edges"].setdefault(("lead", label), []).append(e.sid)
        for label in e.wg_member:
            agg["edges"].setdefault(("member", label), []).append(e.sid)
    # lead supersedes member for the same working group
    for (etype, label) in list(agg["edges"]):
        if etype == "member" and ("lead", label) in agg["edges"]:
            del agg["edges"][(etype, label)]
    return agg


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------

def id_prefix(type_name: str) -> str:
    return type_name.replace(" ", "")


def build_container_nodes(df: pd.DataFrame, type_name: str, spec: dict, warnings: set):
    """Organisation / Working-group nodes. Returns (elements, label->ids map)."""
    elements = []
    label_map: dict[str, list[str]] = {}
    placeholder = config.PERSONAL_ORG_PLACEHOLDER.strip().lower()
    for _, row in df.iterrows():
        row_id = safe_str(row["id"])
        label = safe_str(row[spec["label_field"]])
        if not label:
            warnings.add(f"{type_name} row id={row_id} has an empty label -- skipped.")
            continue
        if type_name == config.TYPE_ORGANISATION and label.lower() == placeholder:
            continue  # placeholder, not a real organisation
        if label == config.OTHER_PLACEHOLDER:
            continue  # "**Other": never a node (affiliated rows are a hard stop)
        kumu_id = f"{id_prefix(type_name)}-{row_id}"
        label_map.setdefault(label, []).append(kumu_id)
        elem = {
            "ID": kumu_id, "Label": label, "Type": type_name,
            "type-display": type_name, "Size": config.NODE_SIZES[type_name],
        }
        src = config.TYPE_DISPLAY_SOURCE_FIELD.get(type_name)
        if src:
            v = safe_str(row.get(src))
            if v:
                elem["type-display"] = v
            else:
                fallback = config.TYPE_DISPLAY_FALLBACK[type_name]
                elem["type-display"] = fallback
                warnings.add(f"{type_name} '{label}' has no '{src}' -- type-display falls back to '{fallback}'.")
        for f in spec["profile_fields"]:
            elem[f] = safe_str(row.get(f))
        elements.append(elem)
    for label, ids in label_map.items():
        if len(ids) > 1:
            warnings.add(f"Ambiguous {type_name} label '{label}' matches {ids} -- edges naming it are skipped.")
    return elements, label_map


def person_profile(agg: dict, public_pseudonymised: bool | None) -> dict:
    """
    public_pseudonymised: None -> internal (all fields); True -> public,
    identifiable fields blanked; False -> public, identifiable tier.
    """
    out = {}
    for f in config.PERSON_FIELD_MAP:
        if public_pseudonymised is not None and f in config.PERSON_INTERNAL_ONLY_FIELDS:
            continue
        if public_pseudonymised and f in config.PERSON_IDENTIFIABLE_FIELDS:
            out[f] = ""
        else:
            out[f] = agg["fields"][f]
    return out


def internal_person_element(key: str, agg: dict, link_status: str, pseudonymised_id: str) -> dict:
    em = agg["emails"]
    uuid_label = key if not key.startswith("email-") else key[len("email-"):]
    return {
        "ID": f"Person-{key}",
        "Label": uuid_label,
        "Type": config.TYPE_PERSON,
        "type-display": config.TYPE_PERSON,
        "Size": config.NODE_SIZES[config.TYPE_PERSON],
        "pseudonymised-id": pseudonymised_id,
        "mapping-consent": agg["consent"],
        "link-status": link_status if not key.startswith("email-") else "unstacked",
        "email-count": str(len(em)),
        "email-ids": join_pipe(e.sid for e in em),
        "on-slack": yes_no(any(e.slack for e in em)),
        "on-jisc": yes_no(any(e.jisc for e in em)),
        "slack-email-ids": join_pipe(e.sid for e in em if e.slack),
        "jisc-email-ids": join_pipe(e.sid for e in em if e.jisc),
        "consent-by-email": join_pipe(f"{e.sid}:{config.CONSENT_CANONICAL[e.rank]}" for e in em),
        "data-user-validation": join_pipe(f"{e.sid}:{yes_no(e.validated)}" for e in em),
        **person_profile(agg, None),
    }


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def person_connections(source_id: str, agg: dict, label_maps: dict, internal: bool,
                       warnings: set, who: str) -> list[dict]:
    conns = []
    target_for = {
        "affiliation": config.TYPE_ORGANISATION,
        "lead": config.TYPE_WORKING_GROUP,
        "member": config.TYPE_WORKING_GROUP,
    }
    for (etype, label), sids in sorted(agg["edges"].items()):
        ttype = target_for[etype]
        candidates = label_maps[ttype].get(label)
        if not candidates:
            warnings.add(f"{who}: names {ttype} '{label}' (sharepoint-id {unique(sids)}) -- no matching row, edge skipped.")
            continue
        if len(candidates) > 1:
            continue  # already warned as ambiguous
        c = {
            "ID": f"{source_id}|{etype}|{candidates[0]}",
            "From": source_id,
            "To": candidates[0],
            "Type": etype,
            "connection-type": etype,  # custom copy of Type, used by Kumu filter/decorations
            "Direction": "undirected",
        }
        if internal:
            c["email-ids"] = join_pipe(unique(sids))
        conns.append(c)
    return conns


# ---------------------------------------------------------------------------
# Kumu display settings (legend limited to the types in use)
# ---------------------------------------------------------------------------
# Kumu lists every colour entry named in element-color: categorize(...),
# whether or not that type occurs in the data. So the entries are generated
# here, from the type-display values actually present, instead of being
# hard-coded. Colours come from config.TYPE_DISPLAY_COLOURS; the legend is sorted alphabetically.

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def load_display_template(references_dir: Path, name: str = DISPLAY_SETTINGS_NAME) -> str:
    path = Path(references_dir) / (name + ".template")
    if not path.exists():
        raise PipelineError(f"Kumu display-settings template not found: {path}")
    text = path.read_text(encoding="utf-8")
    if TYPE_COLOURS_PLACEHOLDER not in text:
        raise PipelineError(f"{path} does not contain the {TYPE_COLOURS_PLACEHOLDER} placeholder.")
    return text


def render_display_settings(template: str, present: list[str], warnings: set) -> str:
    """Substitute #HEX "Value" entries (alphabetical, case-insensitive; present values only)."""
    for value, hex_ in config.TYPE_DISPLAY_COLOURS.items():
        if '"' in value or not _HEX.match(hex_):
            raise PipelineError(
                f"config.TYPE_DISPLAY_COLOURS entry {value!r}: {hex_!r} is invalid "
                f"(value must not contain a double quote; colour must be #RRGGBB)."
            )
    for value in sorted(set(present) - set(config.TYPE_DISPLAY_COLOURS)):
        warnings.add(
            f"type-display value '{value}' is not in config.TYPE_DISPLAY_COLOURS -- "
            f"Kumu will auto-colour it. Add it to the registry."
        )
    entries = [
        f'{hex_} "{value}"'
        for value, hex_ in sorted(config.TYPE_DISPLAY_COLOURS.items(),
                                  key=lambda kv: (kv[0].casefold(), kv[0]))
        if value in set(present)
    ]
    return template.replace(TYPE_COLOURS_PLACEHOLDER, ", ".join(entries))


def write_display_settings(elements: list[dict], template: str, references_dir: Path,
                           warnings: set, name: str = DISPLAY_SETTINGS_NAME) -> Path:
    """Generate references/<name> from the elements of the matching workbook
    (internal or public): the legend lists only the types present in them."""
    present = unique(e["type-display"] for e in elements if e.get("type-display"))
    out = Path(references_dir) / name
    text = render_display_settings(template, present, warnings)
    if PIS_URL_PLACEHOLDER in text:
        warnings.add(
            f"{name}: the text still contains the {PIS_URL_PLACEHOLDER} placeholder -- "
            f"replace it with the Participant Information Sheet link in the template."
        )
    out.write_text(text, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_workbook(path: str, elements: list[dict], connections: list[dict]):
    elements_df = pd.DataFrame(elements)
    connections_df = pd.DataFrame(connections)
    front = [c for c in ["ID", "Label", "Type", "type-display", "Size"] if c in elements_df.columns]
    elements_df = elements_df[front + [c for c in elements_df.columns if c not in front]]
    if len(connections_df):
        cf = [c for c in ["ID", "From", "To", "Type", "connection-type", "Direction"] if c in connections_df.columns]
        connections_df = connections_df[cf + [c for c in connections_df.columns if c not in cf]]
    else:
        connections_df = pd.DataFrame(columns=["ID", "From", "To", "Type", "connection-type", "Direction"])
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        elements_df.to_excel(writer, sheet_name="elements", index=False)
        connections_df.to_excel(writer, sheet_name="connections", index=False)


def load_names(path: str) -> dict[str, str]:
    if not path or not os.path.exists(path):
        return {}
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    for col in ("baserow-UUID-person", "name"):
        if col not in df.columns:
            raise PipelineError(f"{path} must have columns 'baserow-UUID-person' and 'name'.")
    return {safe_str(r["baserow-UUID-person"]).lower(): safe_str(r["name"]) for _, r in df.iterrows() if safe_str(r["name"])}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(input_dir: str, output_dir: str, tag: str, salt_file: str, names_file: str | None,
        references_dir: Path = REFERENCES_DIR) -> dict:
    warnings: set[str] = set()

    print(f"Loading Baserow exports from {input_dir} ...")
    tables = {}
    for stub in (config.STUB_PEOPLE_EMAIL, config.STUB_PEOPLE_STACKED,
                 config.STUB_ORGANISATIONS, config.STUB_WORKING_GROUPS):
        tables[stub] = load_table(input_dir, stub)
        print(f"  loaded {stub}: {len(tables[stub])} row(s)")

    # Hard stops on the data itself: all problems reported together, before the
    # salt is read or anything is built or written.
    preflight(tables)
    display_template = load_display_template(references_dir)
    public_display_template = load_display_template(references_dir, DISPLAY_SETTINGS_PUBLIC_NAME)
    emails = load_emails(tables[config.STUB_PEOPLE_EMAIL], warnings)
    check_positional_lists(tables[config.STUB_PEOPLE_STACKED], emails, warnings)

    salt = load_or_create_salt(salt_file)
    names = load_names(names_file)

    print("Building elements ...")
    org_elems, org_labels = build_container_nodes(
        tables[config.STUB_ORGANISATIONS], config.TYPE_ORGANISATION, config.ORG_NODE, warnings)
    wg_elems, wg_labels = build_container_nodes(
        tables[config.STUB_WORKING_GROUPS], config.TYPE_WORKING_GROUP, config.WG_NODE, warnings)
    label_maps = {config.TYPE_ORGANISATION: org_labels, config.TYPE_WORKING_GROUP: wg_labels}

    groups = build_groups(tables[config.STUB_PEOPLE_STACKED], emails, warnings)

    internal_people, internal_conns = [], []
    public_people, public_conns = [], []
    # Public map: an organisation appears only if at least one person affiliated
    # with it has validated their data (on any of the stacked person's emails) AND consented
    # to a public tier (pseudonymised or identifiable).
    public_org_ids: set[str] = set()
    stats = {"public_pseudonymised": 0, "public_identifiable": 0, "excluded": 0,
             "split_links": 0, "excluded_none": 0, "named": 0,
             "excluded_bounced": 0, "bounced_emails": 0}

    for key, members in groups:
        # Bounced emails are dropped before anything is built: a person whose
        # every email bounced is left off both maps; a person with at least one
        # working email stays, described by their remaining emails only.
        prank = person_consent_rank(members)  # all emails, bounced or not
        person_validated = any(m.validated for m in members)
        kept = [m for m in members if not m.bounced]
        stats["bounced_emails"] += len(members) - len(kept)
        if not kept:
            stats["excluded_bounced"] += 1
            continue
        members = kept
        # ---- internal: the best-guess stacked person
        where = f"person {key}"
        clusters, status = build_clusters(members)
        agg = aggregate(members, where, warnings, prank)
        if agg["rank"] == config.CONSENT_NONE_RANK:
            # Consent "none" on any email: no node and no links on either map.
            stats["excluded_none"] += 1
        else:
            el = internal_person_element(key, agg, status, pseudonymise(key, salt))
            internal_people.append(el)
            internal_conns += person_connections(el["ID"], agg, label_maps, True, warnings, where)
            if status in ("partial", "unconfirmed"):
                warnings.add(
                    f"{where}: email links not confirmed by the user-nominated UUIDs "
                    f"(link-status={status}); public map treats unconfirmed emails as separate people."
                )

        # ---- public: one node per confirmed cluster
        for i, cluster in enumerate(clusters):
            cagg = aggregate(cluster, f"{where} cluster {cluster[0].sid}", set(), prank)  # warned above
            if cagg["rank"] < config.CONSENT_PUBLIC_PSEUDONYMISED_RANK:
                stats["excluded"] += 1
                continue
            pseudo_key = key if i == 0 else f"{key}-{cluster[0].sid}"
            if i > 0:
                stats["split_links"] += 1
            public_id = pseudonymise(pseudo_key, salt)
            identifiable = cagg["rank"] >= config.CONSENT_PUBLIC_IDENTIFIABLE_RANK
            label = public_id
            if identifiable:
                name, _src = resolve_name(cluster, f"{where} cluster {cluster[0].sid}", warnings)
                if name is None and status in ("single", "confirmed") and key in names:
                    name = names[key]  # fallback: reviewed names file
                if name:
                    label = name
                    stats["named"] += 1
                else:
                    warnings.add(
                        f"{where} cluster {cluster[0].sid}: public identifiable but no complete "
                        f"first name + surname -- labelled by pseudonym."
                    )
            stats["public_identifiable" if identifiable else "public_pseudonymised"] += 1
            public_people.append({
                "ID": public_id, "Label": label, "Type": config.TYPE_PERSON,
                "type-display": config.TYPE_PERSON,
                "Size": config.NODE_SIZES[config.TYPE_PERSON],
                **person_profile(cagg, not identifiable),
            })
            pconns = person_connections(public_id, cagg, label_maps, False, set(), where)
            public_conns += pconns
            if person_validated:
                public_org_ids.update(c["To"] for c in pconns if c["Type"] == "affiliation")

    public_org_elems = [o for o in org_elems if o["ID"] in public_org_ids]
    all_org_ids = {o["ID"] for o in org_elems}
    dropped_edges = [c for c in public_conns
                     if c["Type"] == "affiliation" and c["To"] in all_org_ids
                     and c["To"] not in public_org_ids]
    public_conns = [c for c in public_conns if c not in dropped_edges]
    if dropped_edges:
        warnings.add(
            f"{len(dropped_edges)} public affiliation link(s) dropped: the organisation has no "
            f"validated person with public consent, so it is not on the public map."
        )

    internal_elements = internal_people + org_elems + wg_elems
    public_elements = public_people + public_org_elems + wg_elems

    os.makedirs(output_dir, exist_ok=True)
    internal_path = os.path.join(output_dir, f"kumu-import-internal-{tag}.xlsx")
    public_path = os.path.join(output_dir, f"kumu-import-public-{tag}.xlsx")
    write_workbook(internal_path, internal_elements, internal_conns)
    write_workbook(public_path, public_elements, public_conns)
    settings_path = write_display_settings(internal_elements, display_template, references_dir, warnings)
    public_settings_path = write_display_settings(
        public_elements, public_display_template, references_dir, warnings, DISPLAY_SETTINGS_PUBLIC_NAME)

    report_path = os.path.join(output_dir, f"kumu-pipeline-report-{tag}.txt")
    lines = [
        f"Internal: {len(internal_elements)} elements ({len(internal_people)} people), {len(internal_conns)} connections -> {internal_path}",
        f"Public:   {len(public_elements)} elements ({len(public_people)} people), {len(public_conns)} connections -> {public_path}",
        f"Public people: {stats['public_identifiable']} identifiable ({stats['named']} labelled by name), "
        f"{stats['public_pseudonymised']} pseudonymised; "
        f"{stats['excluded']} excluded by consent; {stats['split_links']} split off by unconfirmed email links.",
        f"Public organisations: {len(public_org_elems)} of {len(org_elems)} shown (only those with a validated, "
        f"publicly consenting person); {len(org_elems) - len(public_org_elems)} left off the public map.",
        f"Consent 'none': {stats['excluded_none']} stacked person(s) left off both maps.",
        f"Bounced emails: {stats['bounced_emails']} email(s) ignored; "
        f"{stats['excluded_bounced']} stacked person(s) with no working email left off both maps.",
        f"Kumu display settings (legend: types in use only) -> internal {settings_path}; public {public_settings_path}",
        f"Names file (fallback): {'used (' + str(len(names)) + ' name(s))' if names else 'not supplied'}",
        "",
        f"{len(warnings)} warning(s):",
        *[f"  - {w}" for w in sorted(warnings)],
    ]
    with open(report_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    print(f"\n(report also written to {report_path})")
    return {"internal": internal_path, "public": public_path, "report": report_path,
            "settings": str(settings_path), "settings_public": str(public_settings_path)}


def main():
    parser = argparse.ArgumentParser(description="Build Kumu import workbooks from Baserow CRM exports.")
    parser.add_argument("--input-dir", default="private/crm/baserow-export/2026-10-05",
                        help="Directory containing the four baserow-export-*.xlsx/.csv files.")
    parser.add_argument("--output-dir", default=None,
                        help="Where to write outputs (default: the input directory).")
    parser.add_argument("--salt-file", default="private/crm/pseudonym-salt.txt",
                        help="Fixed path for the pseudonymisation salt (must be stable across runs).")
    parser.add_argument("--names-file", default=None,
                        help=f"Optional CSV (baserow-UUID-person,name) for public-identifiable labels "
                             f"(default: <input-dir>/{config.NAMES_FILE_NAME} if present).")
    parser.add_argument("--references-dir", default=str(REFERENCES_DIR),
                        help="Folder holding the display-settings template; the generated "
                             "settings file is written here (default: the skill's references/ "
                             "folder, which is version-controlled).")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d-%H%M%S"),
                        help="Tag for output filenames (default: the run time, YYYYMMDD-HHMMSS, local time).")
    args = parser.parse_args()

    output_dir = args.output_dir or args.input_dir
    names_file = args.names_file or os.path.join(args.input_dir, config.NAMES_FILE_NAME)
    try:
        run(args.input_dir, output_dir, args.date, args.salt_file, names_file,
            Path(args.references_dir))
    except PreflightStop as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    except PipelineError as e:
        print(f"\nSTOPPED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
