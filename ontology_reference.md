# Ontology Reference — What Each Field Does

The ontology YAML (`config/ontology/<domain>.yaml`) is not a schema registry
or documentation file. It is **executable configuration** — every field has a
specific runtime effect. This reference explains what each field does and what
breaks if it is missing or wrong.

---

## Why the ontology exists

A raw database schema tells the LLM what tables and columns exist.
The ontology tells the system what those tables and columns *mean* and how
they must be queried. The distinction matters because:

- Two tables may contain the same metric calculated differently
- A column called `valor` may be additive in one table and non-additive in another
- The same natural-language phrase ("orders") may map to different tables
  depending on whether the user is asking about forecasts or actuals
- Some tables require mandatory filter predicates — omitting them returns
  incorrect data, not an error

The ontology encodes this knowledge so the system enforces it deterministically,
rather than asking the LLM to infer it each time.

---

## Metric fields

```yaml
- id: metric.orders          # canonical identifier — used in audit logs, scope tags
  kind: metric
  name: orders               # internal name
  display_name: "Orders"     # shown to the user in explanations

  aliases:                   # every phrase that should resolve to this metric
    - orders                 #   ← alias resolution happens in code before LLM call
    - "confirmed orders"     #   ← if missing here, the LLM guesses — inconsistently
    - "order count"

  additivity: additive       # additive | non_additive
                             #   additive   → SUM(valor) across any dimension is valid
                             #   non_additive → metric guard fires if SUM(valor) appears

  grain:
    temporal: DAY            # the finest time unit at which this metric is stored
                             #   DAY   → one row per region per day; date filters use
                             #           equality or range on the raw date column
                             #           (order_date >= '2026-01-01' AND order_date < '2026-02-01')
                             #   MONTH → one row per region per month; date filters must
                             #           truncate to month boundary, not a specific day
                             #           (DATE_TRUNC('month', order_date) = '2026-01-01')
    storage: first_of_period # how the date column physically represents sub-daily periods
                             #   first_of_period → the row's date column holds the 1st day
                             #                     of the period (2026-01-01 = January 2026)
                             #                     filter must use DATE_TRUNC, not a day range
                             #   omit this field for DAY-grain metrics — no convention needed

  binding:
    primary_table_fqn: sales.orders_forecasts_v   # table used for forecast queries
    historical_table_fqn: sales.orders_v          # table used for actuals queries
    value_column: valor                           # column to aggregate

    filter_predicates:                            # REQUIRED filters, always injected
      - "metrica = 'Forecast'"                    #   ← without these, wrong rows return
      - "indicador = 'orders'"                    #   ← LLM would not know to add them

    historical_predicates:                        # filters for the actuals table
      - "indicador = 'orders'"
```

**What breaks if `aliases` is incomplete:** the LLM receives the raw phrase and
resolves it heuristically. Same question asked twice may produce different table
references. The `audit_determinism.md` test catches this.

**What breaks if `filter_predicates` is missing:** the injected prompt does not
include the mandatory filter. The LLM may omit it. The query returns all
`indicador` variants, inflating results by 3–10×. No error is raised.

**What breaks if `additivity` is wrong:** the metric guard in `sql_policy.yaml`
either fires incorrectly (blocking a valid SUM) or fails to fire (allowing an
invalid SUM on a ratio metric). Silent numeric errors result.

**What breaks if `grain` is wrong or omitted:** this is the field most likely
to produce subtly incorrect SQL that passes all other checks. Two failure modes:

- `temporal` set to `DAY` on a MONTH-grain metric — the system generates day-range
  filters (`order_date >= '2026-01-01' AND order_date < '2026-02-01'`) against a
  column that only holds first-of-month values. January returns correctly because
  2026-01-01 falls within the range. A mid-month query returns nothing. No error.

- `storage: first_of_period` omitted on a MONTH-grain metric — the system generates
  a day-range filter instead of `DATE_TRUNC('month', order_date) = '2026-01-01'`.
  Queries for anything other than the exact first day of a month silently return
  zero rows.

The `audit_determinism.md` test surfaces both: run the same month-scoped query
five times with varied phrasing ("January", "last month", "2026-01") and check
whether the generated date predicate is structurally identical across runs.

---

## Dimension fields

```yaml
- id: dimension.region
  kind: dimension
  name: region
  display_name: "Sales Region"

  aliases:
    - region
    - territory
    - area
    - zone

  default_display_attribute: des_region   # column shown in SELECT by default

  attributes:                             # all columns the agent may reference
    - cod_region                          #   ← key column (used in JOIN)
    - des_region                          #   ← readable name
    - des_country

  binding:
    primary_table_fqn: sales.acme_regions
    key_column: cod_region                # join key — used in relation sql_on expressions
    attribute_columns:
      des_region: des_region
      des_country: des_country
```

**What breaks if `default_display_attribute` is absent:** the LLM must decide
which column to SELECT for the dimension. It may choose `cod_region` (a code)
instead of `des_region` (a readable name), producing correct but unreadable output.

**What breaks if `attributes` is incomplete:** `strict_ontology_binding: true`
in `base_agent.yaml` prevents the LLM from referencing columns not declared here.
A missing attribute means the agent cannot answer questions that legitimately
require it — and says so, rather than hallucinating a column name.

---

## Entity fields

Entities represent individual records (an order, a contract) rather than
aggregated metrics. They are used when the user asks about specific items
rather than rolled-up numbers.

```yaml
- id: entity.contract
  kind: entity
  name: contract

  identifier_attribute: cod_contract     # primary key — used in COUNT(DISTINCT ...)

  attributes:
    - cod_contract
    - snapshot_date                      # presence here signals snapshot table behavior
    - status

  binding:
    primary_table_fqn: sales.contracts
    identifier_column: cod_contract
```

The presence of `snapshot_date` in the attributes is the signal that triggers
snapshot fence enforcement in `sql_policy.yaml`. Any query touching this table
must include `snapshot_date = (SELECT MAX(snapshot_date) FROM sales.contracts)`.

---

## Relation fields

Relations declare the valid join paths between metrics, dimensions, and entities.
They prevent the LLM from inventing join conditions.

```yaml
- id: relation.orders_region
  source: metric.orders
  target: dimension.region
  relation_type: joins_on
  sql_on: "orders_forecasts_v.cod_region = acme_regions.cod_region"
```

**What breaks if a relation is missing:** the LLM must infer the join condition
from column names. It will usually guess correctly, but "usually" is not
deterministic. The `audit_determinism.md` test surfaces this as structural
variance across runs.

---

## Scope fields

```yaml
scope:
  domain_description: >
    This assistant answers questions exclusively about Acme Corp sales data...

  out_of_scope_examples:
    - "What is the capital of France?"
    - "Write me a poem"
```

The `domain_description` is used by the Router's scope classifier.
The `out_of_scope_examples` are negative examples that improve the classifier's
precision — they are not shown to the user, they shape the routing decision.

---

## The additivity field in detail

This is the field most likely to cause silent production errors if wrong.

| Value | Meaning | Correct aggregation | What SUM produces |
|---|---|---|---|
| `additive` | Metric can be summed across any dimension | `SUM(valor)` | Correct total |
| `non_additive` | Metric is a snapshot or ratio | Last period's value, or recompute from components | Mathematically wrong — inflated or meaningless |

A metric stored as a daily snapshot (e.g. number of active contracts at end of
day) is non-additive across time: summing January + February + March does not
give you a Q1 figure, it gives you a number 3× too large. The ontology encodes
this once; the metric guard in `sql_policy.yaml` enforces it on every query.

---

## Versioning

Every metric, dimension, entity, and relation carries a `version` field.
Every generated SQL is tagged with the ontology version used:

```sql
-- scope: acme | role: acme_analyst | ontology_version: 1
```

When a wrong answer is reported, the scope tag identifies exactly which version
of which domain file was in effect when the query ran. Debugging a wrong answer
means checking one versioned YAML file, not reconstructing a 40k-token prompt.
