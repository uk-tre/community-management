---
name: crm-kumu-visualisation
description: Convert the UK TRE Community Baserow CRM exports (People-email, People-stacked, Organisations, Working-Groups) into Kumu-ready elements/connections workbooks, internal and public. Use when Cass asks to build, update, or refresh the Kumu map, mentions "kumu export", or provides new baserow-export-*.csv files for this purpose.
---

# CRM to Kumu visualisation (UK TRE Community schema)

Deterministic Python pipeline. No LLM calls are made on the CRM data after
this skill is invoked.
Reading, transforming and writing the data is pure Python (pandas/openpyxl).

## Privacy model

Baserow holds no email addresses. The only names it holds are the two
user-nominated fields `name-first-user-nominated` and
`name-surname-user-nominated` on People-email; they feed the **public
identifiable** label only and never appear on the internal map.
The internal map is labelled by Baserow UUID, and every internal person also
carries the hashed Kumu ID as `pseudonymised-id` (the same salted hash as on the
public map) so the two maps can be matched.
The public map is labelled by a stable hashed pseudonym, except
public-identifiable people with BOTH a first name and a surname, who are labelled
by name (the optional names file is a fallback).

## What it does

1. Reads four exports from the input directory:
   `People-email`, `People-stacked`, `Organisations`, `Working-Groups`
   (`baserow-export-<table>-<date>.csv`; `.xlsx` also works).
2. Stops early, before the salt is read and with nothing written, on
   schema drift; commas in organisation/working-group names
   (`flag-comma-error`); any person affiliated with the `**Other`
   placeholder organisation/working group (comma and `**Other` rows are
   listed together in one message); duplicate or doubly-linked
   sharepoint-ids; or position-aligned lookup lists on People-stacked that do
   not match the linked emails; or a comma in a first name or surname that
   would be shown as a public identifiable label (listed in the same message).
3. Builds nodes: Person (from People-stacked), Organisation, Working group.
   Builds edges: `affiliation`, `lead`, `member`.
4. Rolls consent up per stacked person over all their emails (`None` on any email
   blocks the person; otherwise consent given on any email covers all, the most
   restrictive of those given; emails with `email-bounced` ticked are then
   ignored, but a bounced `None` still counts) and writes two workbooks plus a
   report:
   - `kumu-import-internal-<timestamp>.xlsx`: best-guess stacked people (consent
     `None` excluded; `not-asked` kept), per-email Slack/Jisc detail, full
     profile, `pseudonymised-id`.
   - `kumu-import-public-<timestamp>.xlsx`: public-consent people only,
     hashed IDs and labels (names for identifiable people with both name parts),
     no sharepoint-ids or UUIDs, no notes.
     Organisations appear only if a validated, publicly consenting person is
     affiliated with them.
   - `kumu-pipeline-report-<timestamp>.txt`: counts and warnings.
   - `references/kumu-display-settings-public` (same mechanism, from
     `references/kumu-display-settings-public.template`): public-map filters,
     showcases, sign-up/contact/GDPR text, white background.
   - `references/kumu-display-settings-internal`: the Kumu legend and colour
     code, **generated** from `references/kumu-display-settings-internal.template`
     so that the legend lists only the types present in the data.
     Edit the template, never the generated file.

## Running it

From the repo root:

```
python community-mapping/code/crm-kumu-visualisation/scripts/pipeline.py \
    --input-dir private/crm/baserow-export/2026-10-05
```

Outputs are written next to the inputs.
See `references/how-to-run.md` for the walkthrough and
`references/schema.md` for the rules.

## When the schema changes

1. Re-export the table structure (read-only token, see how-to-run.md).
2. Update `scripts/config.py`, the single registry of columns, field maps,
   consent options, edge types and legend colours
   (`TYPE_DISPLAY_COLOURS`).
3. Re-run the pipeline.
