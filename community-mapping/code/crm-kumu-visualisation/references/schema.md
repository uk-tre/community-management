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
(`not-asked`, `None`, `internal only`, `public PSEUDONYMISED`,
`public IDENTIFIABLE`; compared case-insensitively).
Blank is treated as `not-asked`.
Consent is decided for the **stacked person**, over all their emails (bounced ones included),
and applies to every email of that person, whether or not the email link is confirmed:

1. `None` on any email blocks the whole stacked person from both maps.
2. Otherwise an email that is `not-asked` (or blank) says nothing.
   Consent given on any email covers all of them
   (an address or UUID may have been lost or bounced).
3. If different levels were given, the most restrictive of those given applies.
4. If no email has given consent, the person is `not-asked`.

The same applies to validation for the public-organisation rule below:
a validated email anywhere on the stacked person counts.

| Rolled-up consent | Internal map | Public map |
|---|---|---|
| None | excluded | excluded |
| not-asked / internal only | shown | excluded |
| public PSEUDONYMISED | shown | hashed label, identifiable fields blanked |
| public IDENTIFIABLE | shown | name (both parts needed, else hashed label), profile shown |

Notes, sharepoint-ids, email counts, Slack/Jisc detail and link status are
never in the public workbook at any tier.

## Organisations on the public map

An organisation appears on the public map only if at least one person affiliated with it
has **validated** their data (`data-user-validation` ticked on any of their emails)
**and** consented to a public tier (pseudonymised or identifiable).
Organisations with no such person are left off, and affiliation links to them are dropped
(a public person can therefore appear without that organisation).
Working groups are not gated this way.
The internal map shows every organisation.
The report states how many organisations were shown and left off.

## Bounced emails

Emails with `email-bounced` ticked are ignored before anything is built, on both maps.
A stacked person whose every email has bounced is left off both maps;
a person with at least one working email stays, described by those emails only
(their consent, Slack/Jisc, organisations and working groups come from the remaining emails).
The report states how many emails were ignored and how many people were left off.

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
| career-role-type | email-career-role-type (multi-select) | union, option order, pipe-joined | both public tiers |
| career-stage | email-career-stage | distinct, pipe-joined | both public tiers |
| notes | email-notes-internal | distinct, pipe-joined | never |

Never carried into any output as fields:
every `*-user-nominated` field (unreviewed self-nominated text, for manual
review), and Created by / Last modified by.
The exception is `name-first-user-nominated` and `name-surname-user-nominated`,
used only to build the public identifiable label (first non-empty per part in
sharepoint-id order; both required; names never appear on the internal map).

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

`**Other (not listed below)` (config `OTHER_PLACEHOLDER`; called `**Other` elsewhere in these notes) is a row in both Organisations and
Working-Groups that people choose when their organisation or group is not yet
in the CRM.
It is never shown in Kumu: it produces no node and no edge.
Any People-email row affiliated with it (`email-organisation`, `email-wg-lead`
or `email-wg-member`) stops the run, so the real organisation or group can be
added in Baserow first.
The match is exact (after trimming whitespace) on the full label: `**other` or `**Other` alone do not
match.
The `**Other` row itself, with no one affiliated, does not stop the run.

## Names

No commas in organisation or working-group names, nor in a first name or surname
that would be shown as a public label.
The pipeline checks `flag-comma-error` (config `COMMA_FLAG_FIELD`) and the name
columns, and refuses to run, listing every row (table, row id, name).
Comma rows, `**Other` rows and name-comma rows are reported together, before the salt is read.

## Element conventions

- Person ID: `Person-<UUID>` internal, `Person-<hash>` public.
  Internal people also carry the public hash as `pseudonymised-id`.
  Organisation / working group: `Organisation-<row id>`, `Workinggroup-<row id>`.
- `type-display` drives the legend: Person; organisation `type`;
  working-group `type` (`working-group` / `interest-group`).
  A blank `type` (unconfirmed) gives `organisation-unconfirmed` /
  `working-group-unconfirmed` (`config.TYPE_DISPLAY_FALLBACK`), on both maps, with a warning.
  Colours: `config.TYPE_DISPLAY_COLOURS`; the legend is sorted alphabetically.
  Only values present in the data are written to the generated
  `references/kumu-display-settings-internal` (see how-to-run.md).
- Sizes: Person 1, Working group 50, Organisation 100.
- Pseudonym salt: fixed file (`--salt-file`), keep private and stable.

## Open items

<mark>Suppression list for consent `None` is planned, not yet created (the PIS promises one).</mark>
<mark>Names in Baserow need MFT approval; names of anyone who later chooses `None` must be removed at source.</mark>
