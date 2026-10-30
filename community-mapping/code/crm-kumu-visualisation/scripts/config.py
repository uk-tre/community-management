"""
Schema registry for the CRM -> Kumu export pipeline (UK TRE Community
deployment, schema of 2026-10-05).

This is the ONE file to edit when Baserow's schema changes (renamed/added
fields, new consent options, new edge types). pipeline.py itself should not
need editing for ordinary schema drift.

Data model (privacy constraint for this client)
-----------------------------------------------
No email addresses live in Baserow. Email addresses (and the names taken from
Slack / Jisc sign-up) live in SharePoint on MFT infrastructure; Baserow holds
sharepoint-ids and UUIDs. People-email also holds the first name and surname
a person typed into the form (name-first-user-nominated,
name-surname-user-nominated). The pipeline uses those two fields ONLY for the
label of a PUBLIC IDENTIFIABLE person; they never appear on the internal map
or at any other tier.

  People-email    one row per email address. Primary field is the
                  sharepoint-id-email number. Consent is recorded PER EMAIL
                  (consent-mapping). Organisation / working-group links,
                  Slack / Jisc membership and (for public-identifiable
                  consent) ORCID etc. are all held per email.
  People-stacked  our best guess at grouping emails into one person. Primary
                  field is baserow-UUID-person. Its other columns are lookup
                  (array) formulas over the linked emails.
  Organisations   organisation nodes.
  Working-Groups  working-group / interest-group nodes.

Kumu nodes come from People-stacked + Organisations + Working-Groups.

Per-email attributes are read from the People-email export, joined EXACTLY
on sharepoint-id-email. The positional lookup lists on People-stacked
(emails-slack, emails-jisc, ...) are used as a consistency check only: the
pipeline stops if their lengths disagree, and warns if their order disagrees
with the exact join (see pipeline.check_positional_lists).

Conventions
-----------
- Kumu "Type" = Person / Organisation / Working group.
- "type-display" duplicates Type (or a finer-grained field, see
  TYPE_DISPLAY_SOURCE_FIELD) and drives legend/shape/colour. Kumu treats
  "Type" as a reserved structural field, so display is driven off a
  separate field.
- No commas are allowed in organisation or working-group names (Baserow
  exports multi-value cells as comma-separated text). The pipeline refuses
  to run if one is found.
- "**Other" (an Organisations / Working-Groups row people pick when their
  organisation or group is not yet in the CRM) must not be left in use. The
  pipeline refuses to run while any People-email row is affiliated with it.
  See COMMA_FLAG_FIELD and OTHER_PLACEHOLDER below.
"""

# ---------------------------------------------------------------------------
# Input files: baserow-export-<stub>*.xlsx (or .csv) in the input directory.
# ---------------------------------------------------------------------------

STUB_PEOPLE_EMAIL = "People-email"
STUB_PEOPLE_STACKED = "People-stacked"
STUB_ORGANISATIONS = "Organisations"
STUB_WORKING_GROUPS = "Working-Groups"

# Optional (private) FALLBACK file mapping people to reviewed names, for the
# public map's identifiable tier only. Used only when a person has no complete
# first name + surname in People-email (see NAME_FIELDS). If neither supplies
# a name, a public-identifiable person is labelled with a stable pseudonym.
# CSV with columns: baserow-UUID-person, name
NAMES_FILE_NAME = "names-for-public-map.csv"

# ---------------------------------------------------------------------------
# Expected columns per table (schema-drift detection). The pipeline raises
# on a MISSING column; extra columns are reported and ignored.
# Source: baserow-structure-json-<Table>.json exports, 2026-10-05.
# "id" is the Baserow row id, present in every Baserow row export.
# Staff-identifying columns (Created by, Last modified by) are deliberately
# never read into any output.
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS = {
    STUB_PEOPLE_EMAIL: [
        "id", "sharepoint-id-email", "People-stacked", "baserow-uuid-email",
        "email-organisation", "email-wg-lead", "email-wg-member",
        "slack", "jisc", "consent-mapping",
        "email-orgs-user-nominated", "email-institutional-profile",
        "email-ORCID", "email-github", "email-linkedIn",
        "email-career-role-type", "email-career-stage",
        "name-first-user-nominated", "name-surname-user-nominated",
        "email-notes-internal", "email-notes-user-nominated",
        "email-wg-lead-user-nominated", "email-wg-member-user-nominated",
        "baserow-uuid-email-user-nominated", "data-user-validation",
        "email-socials-user-nominated",
        "email-bounced", "email-bounced-date",
    ],
    STUB_PEOPLE_STACKED: [
        "id", "baserow-UUID-person", "sharepoint-id-emails", "email-count",
        "emails-slack", "emails-jisc", "data-user-validation",
        "emails-consent",
    ],
    STUB_ORGANISATIONS: [
        "id", "name-org", "type", "name-org-long", "name-org-abbreviation",
        "website", "flag-comma-error",
    ],
    STUB_WORKING_GROUPS: [
        "id", "name-DARE-group", "description", "name-DARE-group-long",
        "name-DARE-group-abbreviation", "website", "type",
        "flag-comma-error",
    ],
}

# Baserow formula column that is non-empty when a row's name contains a comma.
# Present on Organisations and Working-Groups (and listed in EXPECTED_COLUMNS
# above). A row with this flag set is a hard stop.
COMMA_FLAG_FIELD = "flag-comma-error"

# Columns also checked directly for commas (belt and braces: the Baserow flag
# only looks at the *-long name, not the abbreviation). Per table.
COMMA_FREE_FIELDS = {
    STUB_ORGANISATIONS: ["name-org", "name-org-long", "name-org-abbreviation"],
    STUB_WORKING_GROUPS: [
        "name-DARE-group", "name-DARE-group-long",
        "name-DARE-group-abbreviation",
    ],
}

# "**Other" is a row in both Organisations and Working-Groups that people can
# be affiliated with when their organisation / group is not yet in the CRM.
# Any People-email row whose columns below name it is a hard stop (fix in
# Baserow, re-export). The match is EXACT (after stripping surrounding
# whitespace): "**other" or "Other" do not match.
# The "**Other" row itself never becomes a node or an edge target.
OTHER_PLACEHOLDER = "**Other (not listed below)"  # exact label of the row in Organisations and Working-Groups
OTHER_CHECK_FIELDS = ["email-organisation", "email-wg-lead", "email-wg-member"]

# ---------------------------------------------------------------------------
# Multi-select fields
# ---------------------------------------------------------------------------
# Baserow exports a multiple-select cell as comma-separated text, but some
# option names themselves contain commas ("Funder, policy-maker or regulator"),
# so the cell cannot be split on commas. The cell is matched against the known
# options instead (longest first). Keep this list in step with Baserow: a
# value not listed here is kept as typed, with a warning (and, if it contains
# a comma, would be split into fragments). Order = output order.

CAREER_ROLE_TYPE_OPTIONS = [
    "TRE team member",
    "Researcher",
    "TRE Host-organisation IT professional",
    "TRE Host-organisation IG professional",
    "Technology or service supplier",
    "Funder, policy-maker or regulator",
    "Data provider or custodian",
    "Public, patient or charity representative",
    "Other",
]

# People-email source field -> its known options.
MULTI_SELECT_FIELDS = {
    "email-career-role-type": CAREER_ROLE_TYPE_OPTIONS,
}

# ---------------------------------------------------------------------------
# Names (public identifiable label only)
# ---------------------------------------------------------------------------
# User-nominated first name and surname on People-email. Combined across a
# person's confirmed-linked emails with the "first non-empty" rule (lowest
# sharepoint-id wins; warning if emails disagree). BOTH parts are needed;
# otherwise the person keeps the hashed pseudonym label. A comma in a name
# that would be used is a hard stop (it would split the Kumu label).

NAME_FIELDS = ("name-first-user-nominated", "name-surname-user-nominated")

# ---------------------------------------------------------------------------
# Node types
# ---------------------------------------------------------------------------

TYPE_PERSON = "Person"
TYPE_ORGANISATION = "Organisation"
TYPE_WORKING_GROUP = "Working group"

NODE_SIZES = {
    TYPE_PERSON: 1,
    TYPE_ORGANISATION: 100,
    TYPE_WORKING_GROUP: 50,
}

# Organisation / working-group node specs: (file stub, label field,
# profile fields carried through as-is).
ORG_NODE = {
    "stub": STUB_ORGANISATIONS,
    "label_field": "name-org",
    "profile_fields": ["type", "website"],
}
WG_NODE = {
    "stub": STUB_WORKING_GROUPS,
    "label_field": "name-DARE-group",
    "profile_fields": ["type", "description", "website"],
}

# type-display granularity: node type -> source field (falls back to the
# plain node type if blank).
TYPE_DISPLAY_SOURCE_FIELD = {
    TYPE_ORGANISATION: "type",
    TYPE_WORKING_GROUP: "type",
}

# type-display value -> hex colour. SINGLE SOURCE OF TRUTH for the Kumu legend:
# the pipeline writes only the values actually present in the data into
# references/kumu-display-settings-internal (from the .template), in THIS
# order. Rebuild from the possible values of Organisations.type and
# Working-Groups.type (Baserow single selects) plus the node-type fallbacks
# used when `type` is blank. Values must not contain a double quote.
TYPE_DISPLAY_COLOURS = {
    TYPE_PERSON: "#000000",
    # Working-Groups.type
    "working-group": "#004488",
    "interest-group": "#6699CC",
    # Organisations.type
    "Funder": "#CC6677",
    "Consortium": "#332288",
    "Academic institute": "#DDCC77",
    "Public sector / Government": "#117733",
    "Intergovernmental organisation": "#88CCEE",
    "Non-profit": "#882255",
    "Social enterprise": "#44AA99",
    "Cooperative": "#999933",
    "Private industry": "#AA4499",
    # Fallbacks when `type` is blank, i.e. not yet confirmed (the pipeline also warns)
    "organisation-unconfirmed": "#DDDDDD",
    "working-group-unconfirmed": "#99AABB",
}

# type-display value used when the source `type` field is blank.
TYPE_DISPLAY_FALLBACK = {
    TYPE_ORGANISATION: "organisation-unconfirmed",
    TYPE_WORKING_GROUP: "working-group-unconfirmed",
}

# ---------------------------------------------------------------------------
# Organisation placeholder
# ---------------------------------------------------------------------------
# Emails without an organisation (e.g. @gmail) are given this affiliation in
# Baserow so that per-email lists stay aligned. It is a PLACEHOLDER: no
# Organisation node and no Kumu edge is created for it. Compared after
# stripping surrounding whitespace, case-insensitively.

PERSONAL_ORG_PLACEHOLDER = "**Personal"

# ---------------------------------------------------------------------------
# Consent (People-email.consent-mapping, single select)
# ---------------------------------------------------------------------------
# Option labels exactly as in Baserow (corrected 2026-10-06 from the earlier
# "IDENITIFIABLE" typo). Comparison is case-insensitive.
#
# Rank = restrictiveness (lower = more restrictive). A person's consent is
# the MINIMUM rank across the emails in the cluster of confirmed-linked
# emails (see pipeline.build_clusters).

CONSENT_RANK = {
    "none": 0,
    "not-asked": 1,
    "internal only": 2,
    "public pseudonymised": 3,
    "public identifiable": 4,
}
CONSENT_CANONICAL = {
    0: "none",
    1: "not-asked",
    2: "internal only",
    3: "public pseudonymised",
    4: "public identifiable",
}
CONSENT_NONE_RANK = 0  # excluded from BOTH workbooks
CONSENT_DEFAULT_RANK = 1  # blank -> not-asked
CONSENT_PUBLIC_PSEUDONYMISED_RANK = 3
CONSENT_PUBLIC_IDENTIFIABLE_RANK = 4

# ---------------------------------------------------------------------------
# Link confirmation
# ---------------------------------------------------------------------------
# Two emails under one People-stacked row are treated as CONFIRMED the same
# person when either email's UUID_NOMINATED_FIELD contains the other's
# UUID_FIELD. Unconfirmed emails are not merged into the person for the
# PUBLIC map (they become separate people with their own consent).
# The internal map always shows the best-guess People-stacked person, with a
# link-status field.

EMAIL_UUID_FIELD = "baserow-uuid-email"
EMAIL_UUID_NOMINATED_FIELD = "baserow-uuid-email-user-nominated"

# ---------------------------------------------------------------------------
# Per-email Person profile fields (People-email source -> Kumu field name)
# and how to combine them across the emails of one person.
#   "first": first non-empty value in sharepoint-id order; a warning is
#            raised if two emails give DIFFERENT non-empty values.
#   "union": distinct values, pipe-joined.
#   "union-options": multi-select field: distinct options across the emails,
#            in config option order, pipe-joined (see MULTI_SELECT_FIELDS).
# ---------------------------------------------------------------------------

PERSON_FIELD_MAP = {
    # kumu field          (source field,                 strategy)
    "ORCID":                ("email-ORCID",                "first"),
    "github":               ("email-github",               "first"),
    "linkedin":             ("email-linkedIn",             "first"),
    "institutional-profile": ("email-institutional-profile", "first"),
    "career-role-type":     ("email-career-role-type",     "union-options"),
    "career-stage":         ("email-career-stage",         "union"),
    "notes":                ("email-notes-internal",       "union"),
}

# Blanked on the PSEUDONYMISED public tier (person appears under a hashed
# label with these fields stripped). Career role type and career stage are
# deliberately NOT here: they are public at both public tiers.
PERSON_IDENTIFIABLE_FIELDS = [
    "ORCID", "github", "linkedin", "institutional-profile",
]

# Internal export only, stripped from the public export at EVERY consent
# tier (including public identifiable).
PERSON_INTERNAL_ONLY_FIELDS = [
    "notes", "email-ids", "email-count", "slack-email-ids", "jisc-email-ids",
    "link-status", "consent-by-email", "data-user-validation",
    "on-slack", "on-jisc",
]

# People-email fields NEVER carried into either export:
#   - every "-user-nominated" field (self-nominated free text, unreviewed
#     staging data for manual review; see references/schema.md), EXCEPT the two
#     NAME_FIELDS, which feed the public identifiable label only,
#   - email-notes-user-nominated, email-socials-user-nominated,
#   - Created by / Last modified by (staff identities).

# ---------------------------------------------------------------------------
# Edge registry (Person -> Organisation / Working group). Source: columns on
# People-email (per email), de-duplicated per person. "lead" supersedes
# "member" for the same person/working-group pair.
# ---------------------------------------------------------------------------

PERSON_EDGE_FIELDS = [
    # People-email field,   target type,          edge type
    ("email-organisation", TYPE_ORGANISATION,    "affiliation"),
    ("email-wg-lead",      TYPE_WORKING_GROUP,   "lead"),
    ("email-wg-member",    TYPE_WORKING_GROUP,   "member"),
]

# Org/WG tables carry the inverse link columns (Organisations.sharepoint-id-key,
# Working-Groups.email-wg-leads / -members). They are skipped to avoid
# duplicate edges.

# ---------------------------------------------------------------------------
# Positional lookup-list checks on People-stacked (see module docstring).
# Boolean/ID lists that must have exactly one entry per linked email.
# ---------------------------------------------------------------------------

STACKED_ID_LIST_FIELD = "sharepoint-id-emails"
STACKED_COUNT_FIELD = "email-count"
STACKED_BOOLEAN_LIST_FIELDS = {
    "emails-slack": "slack",
    "emails-jisc": "jisc",
    "data-user-validation": "validated",   # Email attribute names
}
