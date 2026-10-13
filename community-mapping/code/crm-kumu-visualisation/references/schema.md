# Schema and rules (UK TRE Community, Baserow schema of 2026-10-05)

`scripts/config.py` is the machine-readable version of this file.

## Tables

| Table | One row is | Primary field | Used for |
|---|---|---|---|
| People-email | one email address | `sharepoint-id-email` | per-email consent, links, Slack/Jisc, profile |
| People-stacked | best-guess person | `baserow-UUID-person` | Person nodes; grouping of emails |
| Organisations | organisation | `name-org` (formula) | Organisation nodes |
| Working-Groups | working or interest group | `name-DARE-group` (formula) | Working-group nodes |

Projects, Communities and Interactions no longer exist in this base.
Inverse link columns on Organisations and Working-Groups are ignored
(the edge is read once, from People-email).

## How a person is built

- Emails are grouped by People-email.`People-stacked`.
- Attributes are read from People-email, joined exactly on
  `sharepoint-id-email`.
- The position-aligned lookup lists on People-stacked
  (`emails-slack`, `emails-jisc`, `data-user-validation`) are a **checksum**:
  each must have one entry per entry in `sharepoint-id-emails`,
  or the run stops.
  Where lengths agree but positions disagree with the exact join,
  a warning is raised and the exact join is used.
- Never edit exports in Excel; export directly from Baserow and run.

## Consent

Per email: `consent-mapping`
(`not-asked`, `none`, `internal only`, `public PSEUDONYMISED`,
`public IDENTIFIABLE`).
Blank is treated as `not-asked`.
Person consent = most restrictive among the emails in the cluster.

| Rolled-up consent | Internal map | Public map |
|---|---|---|
| none / not-asked / internal only | shown | excluded |
| public PSEUDONYMISED | shown | hashed label, identifiable fields blanked |
| public IDENTIFIABLE | shown | hashed label (or name if names file), profile shown |

Notes, sharepoint-ids, email counts, Slack/Jisc detail and link status are
never in the public workbook at any tier.

## Confirmed links

Two emails under one People-stacked person are **confirmed** the same person
if either email's `baserow-uuid-email-user-nominated` contains the other's
`baserow-uuid-email`.
`link-status` (internal map): `single`, `confirmed`, `partial`,
`unconfirmed`, `unstacked`.
The internal map always shows the whole stacked person.
The public map splits unconfirmed emails into separate people,
each with its own consent, organisations and groups.

## Fields

| Kumu field | Source (People-email) | Combine | Public |
|---|---|---|---|
| ORCID | email-ORCID | first non-empty; warn on conflict | identifiable tier only |
| github | email-github | as above | identifiable tier only |
| linkedin | email-linkedIn | as above | identifiable tier only |
| institutional-profile | email-institutional-profile | as above | identifiable tier only |
| career-role-title | email-career-role-title | distinct, pipe-joined | identifiable tier only |
| career-stage | email-career-stage | distinct, pipe-joined | both public tiers |
| notes | email-notes-internal | distinct, pipe-joined | never |

Never carried into any output:
every `*-user-nominated` field (unreviewed self-nominated text, for manual
review), and Created by / Last modified by.

## Edges

| Edge | From People-email column | To |
|---|---|---|
| affiliation | email-organisation | Organisation |
| lead | email-wg-lead | Working group |
| member | email-wg-member | Working group |

Duplicates across a person's emails collapse to one edge.
`lead` supersedes `member` for the same group.
Internal edges carry `email-ids` (which emails support the edge).
Edge IDs are `<from>|<type>|<to>`, stable across runs.

## Placeholder organisation

`**Personal` (config `PERSONAL_ORG_PLACEHOLDER`) marks emails with no
organisation (for example gmail).
It produces no node and no edge.

## Names

No commas in organisation or working-group names.
The pipeline checks the names and `flag-comma-error`, and refuses to run.

## Element conventions

- Person ID: `Person-<UUID>` internal, `Person-<hash>` public.
  Organisation / working group: `Organisation-<row id>`, `Workinggroup-<row id>`.
- `type-display` drives the legend: Person; organisation `type`;
  working-group `type` (`working-group` / `interest-group`).
- Sizes: Person 1, Working group 50, Organisation 100.
- Pseudonym salt: fixed file (`--salt-file`), keep private and stable.

## Open items

<mark>Suppression list for consent `none` is planned, not yet created.</mark>
<mark>Names file for public-identifiable labels (SharePoint export) not yet defined.</mark>
