---
name: crm-kumu-visualisation
description: Convert the UK TRE Community Baserow CRM exports (People-email, People-stacked, Organisations, Working-Groups) into Kumu-ready elements/connections workbooks, internal and public. Use when Cass asks to build, update, or refresh the Kumu map, mentions "kumu export", or provides new baserow-export-*.csv files for this purpose.
---

# CRM to Kumu visualisation (UK TRE Community schema)

Deterministic Python pipeline. No LLM calls are made on the CRM data after
this skill is invoked.
Reading, transforming and writing the data is pure Python (pandas/openpyxl).

## Privacy model

Baserow holds no email addresses and no names for this client.
Identifiable data lives in SharePoint; Baserow holds sharepoint-ids and UUIDs.
The internal map is therefore labelled by Baserow UUID.
The public map is labelled by a stable hashed pseudonym, unless an optional
names file supplies a name for a public-identifiable person.

## What it does

1. Reads four exports from the input directory:
   `People-email`, `People-stacked`, `Organisations`, `Working-Groups`
   (`baserow-export-<table>-<date>.csv`; `.xlsx` also works).
2. Stops on schema drift, commas in organisation/working-group names,
   duplicate or doubly-linked sharepoint-ids, or position-aligned lookup
   lists on People-stacked that do not match the linked emails.
3. Builds nodes: Person (from People-stacked), Organisation, Working group.
   Builds edges: `affiliation`, `lead`, `member`.
4. Rolls consent up per person (most restrictive among the confirmed-linked
   emails) and writes two workbooks plus a report:
   - `kumu-import-internal-<date>.xlsx`: best-guess stacked people,
     per-email Slack/Jisc detail, full profile.
   - `kumu-import-public-<date>.xlsx`: public-consent people only,
     hashed IDs and labels, no sharepoint-ids or UUIDs, no notes.
   - `kumu-pipeline-report-<date>.txt`: counts and warnings.

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
   consent options and edge types.
3. Re-run the pipeline.
