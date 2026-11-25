# Stage 25 brief: Analytics engineering

Owner brief, 2026-11-14. This file is the design brief for a new stage. It is not the work
queue. The session turns it into Stage 25 in `docs/BACKLOG.md` (step 1 below), and from then
on BACKLOG.md is what gets worked, as CLAUDE.md says.

## Why this stage exists

The owner is applying for analytics engineering roles as well as data and AI engineering
roles. This repo is his strongest evidence for them, and today it is only partly that.

What analytics engineering postings ask for, taken from the ones he has interviewed for:

- Dimensional data modeling with a stated grain (facts and dimensions).
- dbt used the way a team uses it: enforced model contracts, tests at every layer, unit
  tests, documentation, exposures.
- A metrics or semantic layer, so each business number has one definition.
- The models running on a cloud warehouse (Databricks, Snowflake or BigQuery).
- A git-native workflow: PR checks, SQL linting, CI that builds only what changed.
- Someone downstream actually using the models (a dashboard or report).
- Being able to explain all of it to a non-technical stakeholder.

## What the repo already has (checked on 2026-11-14, commit c07cee8)

Keep all of this. Nothing here is to be rewritten.

- dbt-core 1.12 on Postgres, bronze to silver to gold, built by the feed DAGs.
- A staging contract (`stg_*`) feeding OMOP CDM 5.4 gold, plus the de-identified shareable
  tier with grants.
- File-feed gold tables: `gold.member_eligibility`, `gold.claim_line`,
  `gold.professional_claim_line`, `gold.remittance_line`, `gold.claim_payment`, and the
  finance pack's `gold.vendor_invoice` and `gold.invoice_match`.
- Generic, singular and custom generic tests (DQD-named checks, `unique_combination`), with
  every result written to `ops.dq_results`.
- Lineage to Marquez. Unit, integration and E2E tests in CI on every push.

## What is missing

Each line is a gap a reviewer for an analytics engineering role would notice.

1. No dimensional model. Gold is OMOP tables and one-table-per-feed entities. There is no
   fact table, no dimension table, and no written grain.
2. No metrics layer. A number such as denial rate or paid per member per month is not
   defined anywhere, so two queries could compute it two ways.
3. No enforced dbt model contracts, no dbt unit tests, no exposures, no slowly changing
   dimension. (Incremental models are already planned in 19.2 and stay there.)
4. No SQL linter and no dbt docs site. Model and column descriptions cover part of the
   project only.
5. CI rebuilds everything. There is no build of only the changed models and what depends
   on them.
6. One warehouse. Every model runs on Postgres only.
7. Nothing reads the gold layer as a business user would. Grafana shows platform health,
   not business numbers.

## The stage

**Delivers:** a claims and eligibility star schema on top of the existing gold layer, a
metrics layer with one definition per number, the dbt engineering practices a team expects,
the same marts and tests passing on a cloud warehouse, and one business dashboard that reads
them.

**Features:** dimensional modeling, model contracts, unit tests, a slowly changing
dimension, a metrics layer, exposures, SQL linting, changed-models-only CI, dbt docs, a
second warehouse target.

**Demo on its own:** drop one eligibility file and one claims file. The marts rebuild. Ask
"what was paid per member per month by plan, and what was the denial rate by payer" from the
metrics layer and get the same answer the hand-written check query gives. Open the lineage
graph from a metric back to the landed file. Then run the on-demand job that builds the same
marts on the cloud warehouse and shows the same test results.

**Incident:** the first real defect the stage's own grain or reconciliation tests catch,
written into `docs/incidents.md`. Do not stage one. A join that fans out and double counts
paid dollars is the kind of thing these tests exist to catch.

**Adds no service.** Postgres and Grafana are already in the cluster, so the 12 GB memory
rule is not in play. If any item turns out to need a new service, that item is **(ask)**.

### Items, in order

Core items come first. They are the ones that close the gap for interviews. The owner wants
this repo portfolio-ready by 2026-11-26, so if time runs short, stop after the core items
and say so in PROGRESS.md.

**Core**

- **25.1 Plan and dimensional design (plan only, no models yet).** One PR that adds Stage 25
  to BACKLOG.md from this brief, a line in PLAN.md's decisions log, and
  `docs/analytics-engineering.md` with: the business questions the marts answer, a bus
  matrix (which facts use which dimensions), the grain of every fact in one sentence, a
  Mermaid diagram of the star schema, and the list of metrics with their plain-word
  definitions. Change this brief's item list where the repo's real state calls for it, and
  say what changed and why.
- **25.2 Dimensions.** A `marts` schema. Conformed dimensions built from the existing gold
  and silver models: date, member, plan, payer, provider (billing NPI), procedure, place of
  service. The member dimension is type 2 (valid from, valid to, current flag) so a claim
  joins to the member's plan as of the service date. The eligibility deliveries already
  keep history in silver, so build the type 2 rows from that history if it is more
  reproducible than a dbt snapshot, and use a dbt snapshot where a source overwrites
  itself. Say which was used where and why. Surrogate keys must be stable across rebuilds,
  as the OMOP ids already are.
- **25.3 Facts.** Claim line (one row per claim line, with billed, allowed, paid,
  coinsurance and deductible), claim payment from the 837 and 835 feeds (charge, paid,
  adjustments, payment status, days to payment), and member month (one row per member per
  covered month). Existing gold tables stay as they are and become the facts' inputs.
- **25.4 Contracts and tests on the marts.** Every marts model has an enforced dbt contract
  (column names, types, constraints). Tests for: the grain of each fact is unique, every
  foreign key finds its dimension row, type 2 rows for a member never overlap and leave no
  gap inside coverage, and reconciliation, meaning each fact's row count and dollar totals
  equal the gold table it came from, to the cent. dbt unit tests with fixed input rows for
  the logic that is easy to get wrong: payment status, member month expansion, and the
  type 2 boundaries. Results keep flowing into `ops.dq_results`.
- **25.5 Metrics layer.** Each metric defined once, in version control, and queryable by
  the dimensions above. Start with: member months, paid and allowed per member per month,
  claims per 1,000 members, denial rate by lines and by dollars, average and median days to
  payment, paid to billed ratio, and patient responsibility share. Prefer dbt's semantic
  layer format (semantic models and metrics in YAML, queried with MetricFlow), because
  that is what postings name. Check first that a version exists that works with this
  repo's dbt-core and with Postgres at $0. If it does not, build the same outcome another
  way and write the reason in PLAN.md. Either way, every metric has a test that compares it
  with a hand-written SQL query on the same data.
- **25.6 Documentation a stakeholder can read.** A description on every marts model and
  column, with a check in CI that fails when one is missing. A metric dictionary in
  `docs/analytics-engineering.md`: each metric's definition in plain words, its formula,
  what it leaves out, and its limits, written for a senior leader who is not technical.
  `dbt docs generate` runs in CI and its output is kept as a build artifact.

**Then**

- **25.7 SQL linting and changed-models-only CI.** A SQL linter on the dbt project in the
  `lint` job (existing models may need a baseline, do not reformat the whole project in the
  same PR as turning it on). A PR job that builds and tests only the models a PR changed
  and what depends on them, compared against `main`'s manifest.
- **25.8 A business dashboard and exposures.** One Grafana dashboard that reads the marts
  through a new read-only login limited to the `marts` schema, tested with a second login
  that must be refused elsewhere. It shows the metrics from 25.5, not platform health.
  Grafana reads Prometheus today, so this adds a Postgres data source to the existing
  Grafana, which is configuration and not a new service.
  Declare it, and the metrics report, as dbt exposures so they appear in lineage. Add a
  screenshot to the README.
- **25.9 The marts on a cloud warehouse.** The owner has a Databricks Free Edition
  workspace with a serverless SQL warehouse. Goal: the marts, their tests and the metric
  checks pass there too, from an on-demand workflow (`workflow_dispatch`), with Postgres
  staying the default on every push. Export the marts' inputs for the **synthetic healthcare
  pack only** and load them as source tables. MIMIC data never leaves the laptop, and
  nothing real goes to any cloud. Write the SQL so one model file serves both warehouses,
  with adapter-specific pieces behind macros. Start with a short spike that confirms Free
  Edition allows this connection and what its limits are. If it does not, write that under
  the item with the alternatives and stop. The workspace host, HTTP path and token are the
  owner's to create and store as GitHub secrets (see owner steps).
- **25.10 The finance pack's star (optional).** An invoice fact with vendor and date
  dimensions and two metrics (invoice match rate, days from invoice to due date), to show
  the pattern is not only healthcare. Only after 25.1 to 25.9.

Add one line to 19.2 when Stage 25 lands: the facts from 25.3 are among the models that
become incremental there.

## Rules that are specific to this stage

CLAUDE.md and `docs/SECURITY_STANDARDS.md` apply as always. These are the points where this
stage could trip over them.

- **Do not change what the agents see.** Recordings are fingerprinted on tool results,
  including database rows. Stage 25 adds a new schema and new models. It must not change
  the columns or rows of any existing model, or any file under `docs/runbooks/`. If an item
  cannot be done without that, stop and write it up as **(ask)**.
- **No new agent and no model calls.** The stage costs $0. "Agent evals in CI" is met by
  the existing replay suite staying green.
- **New dependencies need the owner's OK (directive 11),** and each one is checked first as
  directive 10 says, then locked with hashes and the 7 day cooldown like the rest. The
  likely ones: a SQL linter, the metrics layer's package, and the cloud warehouse's dbt
  adapter. Any dbt package (a `packages.yml`) counts as a dependency too.
- **Anything public or hosted is (ask).** Publishing the dbt docs to GitHub Pages is not
  part of this stage unless the owner says so. The artifact in CI is enough.
- **The README stays honest.** It says what ran where and when, for example "the marts and
  their tests also pass on Databricks Free Edition, run on demand, last on <date>". It
  never says "production", "enterprise" or "at scale" for this stage. A checklist line is
  ticked only when its item is merged.
- **Synthetic data in every committed fixture and every screenshot.**

## Owner steps this stage will need

Write each one into `docs/OWNER_STEPS.md` in the usual form (why, PowerShell commands, a
check) when its item comes up. Expected:

1. Approve the new dependencies, named with their versions, when each item reaches them.
2. For 25.9: create a Databricks access token and add the host, HTTP path and token as
   GitHub Actions secrets. The steps never show a secret.
3. For 25.8: look at the dashboard once and say whether a non-technical leader would
   understand it.

## What the owner needs at the end of each item

He will be asked about this work in interviews, so he has to be able to explain it himself.
When an item is done, besides the usual PROGRESS line and demo steps, add to
`docs/analytics-engineering.md`:

- What was built, in plain words, in a few sentences.
- The one design choice that mattered and what the alternative was.
- Two or three questions an interviewer could ask about it, each with a short answer and
  the file where the proof is.

Keep the diagram in that file current. He learns best from diagrams.

## Done means

- A reviewer can open `docs/analytics-engineering.md` and, in five minutes, see the star
  schema, the grain of each fact, the metric definitions, and where each is tested.
- `dbt build` on a fresh database passes with the marts, their contracts, their unit tests
  and their reconciliation tests.
- Each metric returns the same number as its hand-written check query.
- CI lints SQL and builds only what a PR changed.
- The same marts and tests have passed on a cloud warehouse at least once, with the date and
  row counts recorded.
- The README's checklist shows these items, each linked to its proof.
