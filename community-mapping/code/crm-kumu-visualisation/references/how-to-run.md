# How to run the Kumu export pipeline

## 1. Export from Baserow

For each of the four tables, use Baserow's "Export table" as **CSV**
(xlsx export is not available on the free plan),
with no edits afterwards.
Rename each file to the pattern below:

```
baserow-export-People-email-<DATE>.csv
baserow-export-People-stacked-<DATE>.csv
baserow-export-Organisations-<DATE>.csv
baserow-export-Working-Groups-<DATE>.csv
```

Put them in one folder, expected at `private/crm/baserow-export/2026-10-05/`
(relative to the repo root; this is the default input directory).
Do not open and re-save them in Excel or Numbers: the Slack/Jisc design relies on list
order, which direct exports preserve.

Before exporting, check in Baserow that the `flag-comma-error` views are empty.

## 2. Run

From the repo root:

```
python community-mapping/code/crm-kumu-visualisation/scripts/pipeline.py \
    --input-dir private/crm/baserow-export/2026-10-05
```

Options:

```
--input-dir   (default: private/crm/baserow-export/2026-10-05)
--output-dir  (default: the input directory)
--salt-file   (default: private/crm/pseudonym-salt.txt; must stay fixed)
--names-file  (default: <input-dir>/names-for-public-map.csv if present)
--date        (default: today, YYYYMMDD)
```

If the run prints `STOPPED`, nothing was written.
The message names the rows to fix in Baserow; fix there and re-export.

Read the warnings (also in `kumu-pipeline-report-<date>.txt`):
differing ORCIDs across one person's emails, unconfirmed email links,
unknown organisation or group names, unstacked emails, list-order mismatches.

## 3. Import into Kumu

Drag the workbook onto a Kumu map.
IDs are stable across runs, so re-importing updates rather than duplicates.
Use the internal workbook for RCM's own map and the public workbook for
anything shared.
`references/kumu-display-settings-internal` holds the `@controls` and
`@settings` code for the legend and filters.

## Schema check

1. Baserow, account, API tokens: create a read-only token scoped to this database.
2. `GET https://api.baserow.io/api/database/fields/table/{table_id}/` with
   `Authorization: Token <token>`, once per table.
3. Strip `available_collaborators`, check descriptions contain nothing sensitive.
4. Compare with `config.EXPECTED_COLUMNS`, update `config.py`, revoke the token.

## Names file (optional)

A private CSV with columns `baserow-UUID-person,name`, exported from
SharePoint.
Used only to label **public-identifiable** people whose email links are
single or confirmed.
Without it, all public people are labelled by pseudonym.

## Open items

<mark>Names for public-identifiable people: no Baserow fields yet capture
user-nominated first and last names for people who consent to identifiable mapping.
Names are currently held only in the SharePoint lists, taken from Slack/Jisc sign-up
information.
That data is incomplete and first/last names are not separated.
Until resolved, the public map labels everyone by pseudonym
(the optional names file, `baserow-UUID-person,name`, is the interim route).</mark>

<mark>SharePoint duplicate-email check: no unique-field check on email address has
been created in the SharePoint lists yet.
This underpins the one-row-per-email assumption (one sharepoint-id per email).</mark>

<mark>MS365 form data not yet integrated: identifiable information (ORCID, etc.)
supplied through the MS365 form by people with internal-only or pseudonymised consent
is not read by the pipeline.
Proposed: add a further pipeline step that takes an export of the MS365 form
responses, matches them on the Baserow UUID the respondent supplied, and merges the
identifiable fields into the INTERNAL workbook only.
Design needed: matching and reconciliation against the SharePoint list,
handling of unmatched or duplicate responses, and a manual-review output.</mark>

<mark>Kumu as a place to hold identifiable data: the internal map would hold
identifiable fields from the MS365 form.
MFT has a paid private Kumu licence, but it must be confirmed that Kumu is an
acceptable place to hold this data for this client (NHS-linked project) before the
step above is built.</mark>
