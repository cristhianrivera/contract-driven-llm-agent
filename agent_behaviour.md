# Agent Behaviour — What You Get, What You Control, What You Can't Override

This document is written for someone building their own analytics agent on this
pattern. It answers three questions in order:

1. What does the agent always do, regardless of how you configure it?
2. What does the agent never do, regardless of what the user asks?
3. For each configurable behaviour — which YAML field controls it, and what
   guarantee does declaring it buy you?

The goal is to make the ontology legible as a **control surface**, not just a
schema registry. Every field you declare is a commitment the system makes about
how it will behave. This document maps those commitments explicitly.

---

## What the agent always does

These are invariants. They hold for every query, every tenant, every domain.
They are not prompt instructions — they are enforced structurally.

**Resolves concepts before calling the LLM.**
Natural-language phrases are matched against the `aliases` lists in the ontology
and pinned to canonical metric and dimension IDs in code. The LLM receives
pre-resolved bindings, not raw user phrasing. This means "confirmed orders",
"order count", and "total orders" all produce the same SQL — not because the
model is consistent, but because they all resolve to `metric.orders` before the
model is involved.

**Injects mandatory filter predicates.**
Every `filter_predicates` entry declared in a metric binding is injected into
the prompt as a required constraint. The LLM cannot omit them. A metric that
requires `indicador = 'orders'` will always produce SQL containing that predicate,
regardless of how the user phrased the question.

**Tags every generated query with scope and ontology version.**
Every SQL output includes a comment header:
```sql
-- scope: acme | role: acme_analyst | ontology_version: 1
```
This is not cosmetic. It is the audit trail that makes debugging tractable —
a wrong answer can be traced to the exact version of the ontology that was in
effect when the query ran.

**Asks before assuming.**
When a query is ambiguous — no time range specified, metric alias maps to more
than one concept — the agent asks one clarifying question rather than generating
SQL that silently picks an interpretation. This is declared in `base_agent.yaml`
under `ambiguity_strategy: ask_one_question` and enforced by the pre-generation
guardrail `G_PRE_003`.

**Explains empty results.**
If a query executes successfully but returns zero rows, the agent does not
silently return nothing. It explains why the result may be empty and suggests
a relaxed query. This is `G_POST_003` in `guardrails.yaml`.

**Screens PII in both directions.**
User queries are scanned for PII patterns before being forwarded to the LLM
(`G_PRE_002`). Query results are scanned before being returned to the user
(`G_POST_004`). Both are configurable pattern lists — see `guardrails.yaml`.

---

## What the agent never does

These are hard refusals. They cannot be overridden by prompt content, user
phrasing, or creative question framing. They are either enforced by the
Reconciler (pure code, post-generation) or by structural isolation at load time.

**Never invents schema objects.**
`strict_ontology_binding: true` in `base_agent.yaml` means the LLM may only
reference tables, columns, metrics, and dimensions declared in the loaded
ontology. If a concept is not declared, the agent says so. It does not guess
a plausible-sounding column name.

**Never SUMs a non-additive metric.**
Metrics declared with `additivity: non_additive` are protected by a metric guard
in `sql_policy.yaml`. If the generated SQL contains `SUM(valor)` for a ratio or
snapshot metric, the Reconciler blocks it and retries with a specific hint. The
retry prompt includes the correct computation — recompute from components, or
use the last period's value — not just a rejection.

**Never queries a snapshot table without a temporal fence.**
Tables that store one row per entity per snapshot date will return every
historical version of each row if queried without a fence. The Reconciler checks
for the required `snapshot_date = (SELECT MAX(snapshot_date) FROM <table>)`
predicate and blocks any query that omits it.

**Never crosses tenant scope.**
Each tenant's ontology is loaded into an isolated scope object at session start.
The router resolves the scope from the authenticated user's role prefix and does
not merge scopes. A table declared in Tenant A's ontology is unreachable in a
Tenant B session — not because a prompt says so, but because it was never loaded.
`cross_scope_reference: block` in `base_agent.yaml` instructs the Reconciler to
catch any reference that escapes this isolation.

**Never answers out-of-scope questions.**
The router scores every incoming query against the domain scope declared in the
ontology. Below the 0.80 confidence threshold, the query is declined before the
LLM is called. The agent does not attempt to answer general knowledge questions,
perform creative tasks, or query tables outside the loaded ontology.

**Never fabricates an answer when data is unavailable.**
`fabrication: block` in `base_agent.yaml` means the agent must say "I don't
have data for that" rather than generating a query that would return empty or
misleading results. Combined with `uncertainty_disclosure: required`, the agent
cannot silently pick an interpretation when it is not certain which metric or
dimension the user means.

---

## What you control — the ontology as a control surface

This section maps each configurable behaviour to the YAML field that controls it.
Declaring the field is not documentation — it is a commitment the system enforces.

---

### Metric resolution and alias coverage

**Field:** `metrics[*].aliases`
**File:** `config/ontology/<domain>.yaml`

```yaml
aliases:
  - orders
  - "confirmed orders"
  - "order count"
```

**What declaring this buys you:** every phrase in this list resolves to the same
canonical metric binding before the LLM is called. Resolution is deterministic —
the same phrase produces the same table, the same predicates, the same SQL
structure on every run.

**What omitting it costs you:** the LLM infers the mapping from phrasing on each
call. Structurally similar queries with different wording produce different SQL.
The `audit_determinism.md` test surfaces this as variance across runs at
temperature=0 — a clear signal that the binding is incomplete.

---

### Mandatory filter predicates

**Field:** `metrics[*].binding.filter_predicates`
**File:** `config/ontology/<domain>.yaml`

```yaml
filter_predicates:
  - "metrica = 'Forecast'"
  - "indicador = 'orders'"
```

**What declaring this buys you:** these predicates are injected into the
generator prompt as required constraints. They appear in every query against
this metric, regardless of what the user asked. A fact view that stores multiple
metric variants in the same table — distinguished by an `indicador` column —
will always be filtered to the correct variant.

**What omitting it costs you:** the LLM may omit the filter. The query returns
all variants in the table, inflating results by the number of distinct
`indicador` values. No error is raised. The numbers look plausible.

---

### Aggregation safety for non-additive metrics

**Field:** `metrics[*].additivity`
**File:** `config/ontology/<domain>.yaml`

```yaml
additivity: non_additive
```

**What declaring this buys you:** the metric guard in `sql_policy.yaml` fires
if the generated SQL aggregates this metric with `SUM(valor)`. The Reconciler
blocks the query and retries with a specific correction — recompute from
components, or use the last period's value. The guard runs in code, not in the
LLM prompt, so it catches the violation even if the model ignores the instruction.

**What omitting it (or setting it wrong) costs you:** silent numeric errors.
A warehouse occupancy metric SUMmed across months returns a number 12× too large.
An active contract count SUMmed across quarters returns a number 3× too large.
Both look like valid query results.

---

### Correct date filter generation

**Field:** `metrics[*].grain.temporal` and `metrics[*].grain.storage`
**File:** `config/ontology/<domain>.yaml`

```yaml
grain:
  temporal: MONTH
  storage: first_of_period
```

**What declaring this buys you:** date filters are generated to match the
physical storage convention. A MONTH-grain metric with `storage: first_of_period`
produces `DATE_TRUNC('month', order_date) = '2026-01-01'` — not a day range that
happens to include the first of the month. A DAY-grain metric produces a range
filter. The LLM is told which convention applies; it does not guess.

**What omitting `storage` costs you:** for MONTH-grain metrics, the system
generates a day-range filter. Queries for the exact first day of a month return
correctly. Queries for any other phrasing of the same month return zero rows.
No error is raised.

---

### Snapshot table protection

**Field:** declared implicitly by `entity[*].attributes` containing `snapshot_date`
**Enforced by:** `sql_policy.yaml` → `require_predicates`

```yaml
require_predicates:
  - table: "sales.contracts"
    predicate: "snapshot_date = (SELECT MAX(snapshot_date) FROM sales.contracts)"
```

**What declaring this buys you:** the Reconciler checks every query against this
table for the temporal fence. A query that omits it is blocked and retried with
the correct predicate injected. This runs in code — it does not rely on the LLM
remembering to add the fence.

**What omitting it costs you:** snapshot tables queried without a fence return
every historical version of each row. A contracts table with 18 monthly snapshots
returns 18× the expected row count. Aggregations over it produce numbers 18×
too large. Looks like data.

---

### Join path validity

**Field:** `relations[*].sql_on`
**File:** `config/ontology/<domain>.yaml`

```yaml
sql_on: "orders_forecasts_v.cod_region = acme_regions.cod_region"
```

**What declaring this buys you:** the join condition between metric and dimension
is resolved from the ontology, not inferred by the LLM from column name
similarity. The generated SQL uses the declared join key — not a guess that
happens to work 90% of the time.

**What omitting it costs you:** the LLM infers join conditions from column names.
It is correct often enough to be misleading. The `audit_determinism.md` test
surfaces this as structural variance — different runs produce different join
conditions on the same query.

---

### Scope definition and out-of-scope handling

**Field:** `scope.domain_description` and `scope.out_of_scope_examples`
**File:** `config/ontology/<domain>.yaml`

```yaml
scope:
  domain_description: >
    This assistant answers questions exclusively about Acme Corp sales data...
  out_of_scope_examples:
    - "What is the capital of France?"
    - "Write me a poem"
```

**What declaring this buys you:** the router uses `domain_description` to score
incoming queries. `out_of_scope_examples` are negative examples that sharpen the
classifier's boundaries — they are not shown to users, they shape the routing
decision. Queries scoring below 0.80 are declined before the LLM is called.

**What omitting it costs you:** the scope boundary is undefined. The router has
no basis for rejection. The LLM receives queries it was not built to answer and
generates best-effort responses against schema it was not configured for.

---

### SQL complexity limits

**Field:** `default.max_joins`, `default.max_ctes`, `scopes[*].forbidden_patterns`
**File:** `config/sql_policy.yaml`

```yaml
default:
  max_joins: 3
  max_ctes: 4
  forbidden_patterns:
    - pattern: "FULL OUTER JOIN"
    - pattern: "SELECT *"
```

**What declaring this buys you:** the Reconciler enforces complexity limits and
forbidden patterns in code, post-generation. SQL that exceeds the join limit or
contains a forbidden pattern is blocked and retried. Limits prevent the model
from generating runaway queries against large schemas; forbidden patterns enforce
structural conventions that the LLM would otherwise violate occasionally.

---

## Reading this as an implementer

If you are mapping your own domain to this pattern, the sequence is:

1. **Declare your metrics with aliases, filter_predicates, additivity, and grain.**
   These four fields together buy you correct resolution, correct filtering,
   correct aggregation, and correct date handling. Any one missing is a silent
   failure mode waiting to appear in production.

2. **Declare your dimensions with attributes and relations.**
   Attributes bound the column space the LLM can reference. Relations fix the
   join paths. Both eliminate a class of heuristic inference that produces
   variance across runs.

3. **Declare your snapshot tables in sql_policy.yaml with temporal fence
   predicates.** One line per snapshot table. The Reconciler handles the rest.

4. **Write your scope description and out-of-scope examples.**
   The more specific the description and the more diverse the negative examples,
   the sharper the routing boundary.

5. **Run audit_runner.py against the audit prompts before going to production.**
   The contract adherence audit (`audit_contract_adherence.md`) tests each
   business rule. The determinism audit (`audit_determinism.md`) tests whether
   your ontology bindings are complete enough to produce consistent SQL across
   runs. Any variance at temperature=0 points to a specific missing field.

The pattern does not make a bad ontology safe. It makes a well-specified
ontology deterministically enforceable — which is a different and more useful
guarantee than a model that tries its best.
