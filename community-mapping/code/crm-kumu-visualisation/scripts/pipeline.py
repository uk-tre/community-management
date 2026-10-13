#!/usr/bin/env python3
"""
CRM (Baserow) -> Kumu export pipeline, UK TRE Community schema (2026-10-05).

Reads four Baserow exports (People-email, People-stacked, Organisations,
Working-Groups), builds Kumu-ready elements + connections workbooks, and
writes TWO versions:

  kumu-import-internal-<date>.xlsx  Every person, as their best-guess
                                    People-stacked node, labelled by Baserow
                                    UUID (no names exist in Baserow). Full
                                    profile, per-email Slack/Jisc detail.
  kumu-import-public-<date>.xlsx    Consent-filtered. Only people whose
                                    (rolled-up) consent is public appear.
                                    Everyone is labelled by a stable hashed
                                    pseudonym unless an optional names file
                                    supplies a name for a public-identifiable
                                    person. No sharepoint-ids, no UUIDs.

Deterministic, pure Python (pandas + openpyxl). No LLM calls.

Run from the repo root:

    python community-mapping/code/crm-kumu-visualisation/scripts/pipeline.py \
        --input-dir private/crm/baserow-export/2026-10-05

Outputs are written to the input directory unless --output-dir is given.
The pseudonymisation salt lives at a FIXED path (--salt-file, default
private/crm/pseudonym-salt.txt) so pseudonyms stay stable across dated
export folders.

The pipeline STOPS (non-zero exit, nothing written) on:
  - missing expected columns (schema drift),
  - commas in organisation / working-group names,
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
import secrets
import sys
from datetime import date

import pandas as pd

import config


class PipelineError(Exception):
    """Raised for conditions that must stop the run."""


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

def check_commas(tables: dict[str, pd.DataFrame]) -> None:
    problems = []
    for stub, fields in config.COMMA_FREE_FIELDS.items():
        df = tables[stub]
        for _, row in df.iterrows():
            for f in fields:
                if "," in safe_str(row[f]):
                    problems.append(f"{stub} id={safe_str(row['id'])} field '{f}': '{safe_str(row[f])}'")
            if safe_str(row["flag-comma-error"]):
                problems.append(f"{stub} id={safe_str(row['id'])}: flag-comma-error is set")
    if problems:
        raise PipelineError(
            "Commas found in organisation / working-group names (these break "
            "multi-value parsing). Fix in Baserow and re-export:\n  - "
            + "\n  - ".join(sorted(set(problems)))
        )


class Email:
    """One People-email row."""

    def __init__(self, row: pd.Series, warnings: set):
        self.sid = safe_str(row["sharepoint-id-email"])
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
        placeholder = config.PERSONAL_ORG_PLACEHOLDER.strip().lower()
        self.orgs = [
            o for o in split_multi(row["email-organisation"]) if o.strip().lower() != placeholder
        ]
        self.wg_lead = split_multi(row["email-wg-lead"])
        self.wg_member = split_multi(row["email-wg-member"])
        self.fields = {
            kumu_name: safe_str(row[src])
            for kumu_name, (src, _strategy) in config.PERSON_FIELD_MAP.items()
        }

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


def aggregate(members: list[Email], where: str, warnings: set) -> dict:
    """Combine one person's (or cluster's) emails into a single record."""
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
                warnings.add(f"{type_name} '{label}' has no '{src}' -- type-display falls back to '{type_name}'.")
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


def internal_person_element(key: str, agg: dict, link_status: str) -> dict:
    em = agg["emails"]
    uuid_label = key if not key.startswith("email-") else key[len("email-"):]
    return {
        "ID": f"Person-{key}",
        "Label": uuid_label,
        "Type": config.TYPE_PERSON,
        "type-display": config.TYPE_PERSON,
        "Size": config.NODE_SIZES[config.TYPE_PERSON],
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
            "Direction": "undirected",
        }
        if internal:
            c["email-ids"] = join_pipe(unique(sids))
        conns.append(c)
    return conns


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_workbook(path: str, elements: list[dict], connections: list[dict]):
    elements_df = pd.DataFrame(elements)
    connections_df = pd.DataFrame(connections)
    front = [c for c in ["ID", "Label", "Type", "type-display", "Size"] if c in elements_df.columns]
    elements_df = elements_df[front + [c for c in elements_df.columns if c not in front]]
    if len(connections_df):
        cf = [c for c in ["ID", "From", "To", "Type", "Direction"] if c in connections_df.columns]
        connections_df = connections_df[cf + [c for c in connections_df.columns if c not in cf]]
    else:
        connections_df = pd.DataFrame(columns=["ID", "From", "To", "Type", "Direction"])
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

def run(input_dir: str, output_dir: str, tag: str, salt_file: str, names_file: str | None) -> dict:
    warnings: set[str] = set()

    print(f"Loading Baserow exports from {input_dir} ...")
    tables = {}
    for stub in (config.STUB_PEOPLE_EMAIL, config.STUB_PEOPLE_STACKED,
                 config.STUB_ORGANISATIONS, config.STUB_WORKING_GROUPS):
        tables[stub] = load_table(input_dir, stub)
        print(f"  loaded {stub}: {len(tables[stub])} row(s)")

    check_commas(tables)
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
    stats = {"public_pseudonymised": 0, "public_identifiable": 0, "excluded": 0, "split_links": 0}

    for key, members in groups:
        # ---- internal: the best-guess stacked person
        where = f"person {key}"
        clusters, status = build_clusters(members)
        agg = aggregate(members, where, warnings)
        el = internal_person_element(key, agg, status)
        internal_people.append(el)
        internal_conns += person_connections(el["ID"], agg, label_maps, True, warnings, where)
        if status in ("partial", "unconfirmed"):
            warnings.add(
                f"{where}: email links not confirmed by the user-nominated UUIDs "
                f"(link-status={status}); public map treats unconfirmed emails as separate people."
            )

        # ---- public: one node per confirmed cluster
        for i, cluster in enumerate(clusters):
            cagg = aggregate(cluster, f"{where} cluster {cluster[0].sid}", set())  # warned above
            if cagg["rank"] < config.CONSENT_PUBLIC_PSEUDONYMISED_RANK:
                stats["excluded"] += 1
                continue
            pseudo_key = key if i == 0 else f"{key}-{cluster[0].sid}"
            if i > 0:
                stats["split_links"] += 1
            public_id = pseudonymise(pseudo_key, salt)
            identifiable = cagg["rank"] >= config.CONSENT_PUBLIC_IDENTIFIABLE_RANK
            label = public_id
            if identifiable and status in ("single", "confirmed") and key in names:
                label = names[key]
            stats["public_identifiable" if identifiable else "public_pseudonymised"] += 1
            public_people.append({
                "ID": public_id, "Label": label, "Type": config.TYPE_PERSON,
                "type-display": config.TYPE_PERSON,
                "Size": config.NODE_SIZES[config.TYPE_PERSON],
                **person_profile(cagg, not identifiable),
            })
            public_conns += person_connections(public_id, cagg, label_maps, False, set(), where)

    internal_elements = internal_people + org_elems + wg_elems
    public_elements = public_people + org_elems + wg_elems

    os.makedirs(output_dir, exist_ok=True)
    internal_path = os.path.join(output_dir, f"kumu-import-internal-{tag}.xlsx")
    public_path = os.path.join(output_dir, f"kumu-import-public-{tag}.xlsx")
    write_workbook(internal_path, internal_elements, internal_conns)
    write_workbook(public_path, public_elements, public_conns)

    report_path = os.path.join(output_dir, f"kumu-pipeline-report-{tag}.txt")
    lines = [
        f"Internal: {len(internal_elements)} elements ({len(internal_people)} people), {len(internal_conns)} connections -> {internal_path}",
        f"Public:   {len(public_elements)} elements ({len(public_people)} people), {len(public_conns)} connections -> {public_path}",
        f"Public people: {stats['public_identifiable']} identifiable, {stats['public_pseudonymised']} pseudonymised; "
        f"{stats['excluded']} excluded by consent; {stats['split_links']} split off by unconfirmed email links.",
        f"Names file: {'used (' + str(len(names)) + ' name(s))' if names else 'not supplied -- all public people labelled by pseudonym'}",
        "",
        f"{len(warnings)} warning(s):",
        *[f"  - {w}" for w in sorted(warnings)],
    ]
    with open(report_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    print(f"\n(report also written to {report_path})")
    return {"internal": internal_path, "public": public_path, "report": report_path}


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
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"),
                        help="Date tag for output filenames (default: today, YYYYMMDD).")
    args = parser.parse_args()

    output_dir = args.output_dir or args.input_dir
    names_file = args.names_file or os.path.join(args.input_dir, config.NAMES_FILE_NAME)
    try:
        run(args.input_dir, output_dir, args.date, args.salt_file, names_file)
    except PipelineError as e:
        print(f"\nSTOPPED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
