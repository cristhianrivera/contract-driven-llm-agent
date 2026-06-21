# How a Query Works — End to End

This document traces a single natural-language question through every layer
of the system. It exists to answer the question that the architecture diagram
alone does not: **why does the ontology matter, and what would break without it?**

---

## The question

> "Show me orders by region for January."

The user is an analyst authenticated with the role `acme_analyst`.

---

## Step 1 — Router: scope resolution

The router reads the role prefix (`acme_`) and loads the corresponding ontology
file: `config/ontology/acme.yaml`.

This file is now the **only schema the system can see for this session**.
No other tenant's tables, columns, or metric definitions are reachable —
not because of a prompt instruction, but because they were never loaded.

The router also runs a scope check: is this question about Acme Corp data?
Score exceeds the 0.80 threshold. The query proceeds.

---

## Step 2 — Alias resolution: "orders" → `metric.orders`

Before the LLM is called, the system scans the user's query for known aliases
declared in the ontology.

"orders" matches the alias list for `metric.orders`:

```yaml
aliases:
  - orders
  - "confirmed orders"
  - "order count"
  - "total orders"
```

The system resolves the alias to the canonical metric ID: **`metric.orders`**.

This step happens in code, not in the LLM prompt. The model never sees "orders"
as a raw word and tries to guess which table it maps to. By the time the LLM
is called, the concept has already been pinned to a specific binding.

---

## Step 3 — Binding resolution: `metric.orders` → concrete SQL ingredients

The system reads the binding block for `metric.orders`:

```yaml
binding:
  primary_table_fqn: sales.orders_forecasts_v
  historical_table_fqn: sales.orders_v
  value_column: valor
  grain_columns:
    day: order_date
  filter_predicates:
    - "metrica = 'Forecast'"
    - "indicador = 'orders'"
  historical_predicates:
    - "indicador = 'orders'"
```

It also reads the dimension binding for `dimension.region`:

```yaml
binding:
  primary_table_fqn: sales.acme_regions
  key_column: cod_region
  attribute_columns:
    des_region: des_region
```

And the relation that joins them:

```yaml
sql_on: "orders_forecasts_v.cod_region = acme_regions.cod_region"
```

The system now has everything the LLM needs to generate correct SQL —
without the model having to infer any of it from a raw DDL dump.

---

## Step 4 — LLM call: generation with resolved context

The LLM receives a prompt that includes the pre-resolved binding, not the raw
schema. Schematically:

```
You are an analytics agent for the acme domain.

The user asked: "Show me orders by region for January."

Resolved metric: metric.orders
  Table: sales.orders_forecasts_v
  Value column: valor
  Required filters: metrica = 'Forecast', indicador = 'orders'
  Latest vintage: WHERE fecha_generacion = (SELECT MAX(fecha_generacion) FROM sales.orders_forecasts_v)

Resolved dimension: dimension.region
  Table: sales.acme_regions
  Display column: des_region
  Join: orders_forecasts_v.cod_region = acme_regions.cod_region

Additivity: additive — SUM(valor) is correct for this metric.
Time range from query: January 2026 → order_date >= '2026-01-01' AND order_date < '2026-02-01'

Generate SQL only. List columns explicitly. Tag the scope header.
```

The LLM generates:

```sql
-- scope: acme | role: acme_analyst | ontology_version: 1
SELECT
    r.des_region,
    SUM(f.valor) AS orders
FROM sales.orders_forecasts_v f
JOIN sales.acme_regions r ON f.cod_region = r.cod_region
WHERE f.metrica = 'Forecast'
  AND f.indicador = 'orders'
  AND f.fecha_generacion = (SELECT MAX(fecha_generacion) FROM sales.orders_forecasts_v)
  AND f.order_date >= '2026-01-01'
  AND f.order_date < '2026-02-01'
GROUP BY r.des_region
ORDER BY orders DESC
```

The model did not guess the table name. It did not invent a filter predicate.
It did not decide whether SUM was valid. All of that was resolved before the
LLM call and handed to the model as constraints, not suggestions.

---

## Step 5 — Reconciler: deterministic policy check

The Reconciler runs the generated SQL against `config/sql_policy.yaml`.
No LLM is involved in this step.

Checks run (all pass in this case):

| Rule | Check | Result |
|---|---|---|
| `forbidden_patterns` | `FULL OUTER JOIN` present? | ✓ No |
| `forbidden_patterns` | `UNION ALL` present? | ✓ No |
| `forbidden_patterns` | `SELECT *` present? | ✓ No |
| `require_predicates` | `sales.contracts` queried without snapshot fence? | ✓ Not referenced |
| `metric_guards` | `SUM(valor)` on a non-additive metric? | ✓ `metric.orders` is additive |
| `temporal_guards` | `orders_forecasts_v` uses latest vintage? | ✓ `fecha_generacion = MAX(...)` present |

All checks pass. The SQL proceeds to the Parallel Validator.

---

## Step 6 — Parallel Validator: semantic correctness check

A second LLM call receives only:
- The generated SQL
- The relevant ontology excerpt (metric binding, dimension binding, additivity rule)

It checks three things:

1. **Column existence** — do `valor`, `cod_region`, `des_region`, `order_date`,
   `fecha_generacion` all appear in the declared schema? Yes.

2. **Aggregation correctness** — `metric.orders` is `additive`, so `SUM(valor)`
   across regions and days is mathematically valid. Pass.

3. **Filter predicate completeness** — the binding requires `metrica = 'Forecast'`
   and `indicador = 'orders'`. Both are present. Pass.

Verdict: **PASS**. The response is returned to the user.

---

## What breaks without the ontology

To make the ontology's role concrete, here is what would go wrong if the LLM
received a raw schema dump instead of pre-resolved bindings:

| Problem | Without ontology | With ontology |
|---|---|---|
| **Table selection** | Model picks `orders_v` or `orders_forecasts_v` based on phrasing heuristics | Binding declares which table is canonical for this query type |
| **Filter predicates** | Model omits `indicador = 'orders'` — returns all indicadores, inflating results | `filter_predicates` are injected as required constraints |
| **Forecast vintage** | Model queries all vintages — returns N× row count | `temporal_guards` require `fecha_generacion = MAX(...)` |
| **Non-additive metrics** | Model SUMs a ratio metric — returns a mathematically wrong number | `additivity: non_additive` triggers metric guard, blocks and retries |
| **Column names** | Model invents plausible-sounding column names not in the schema | `strict_ontology_binding: true` blocks hallucinated columns |
| **Alias resolution** | "confirmed orders" might resolve to a different table on each run | Alias is pinned to `metric.orders` in code before the LLM call |

The ontology is not documentation. It is the structured input that makes the
LLM's output deterministic and auditable. Remove it, and you have a system
that works most of the time and fails silently the rest.

---

## The query in one diagram

```
"Show me orders by region for January"
        │
        ▼
┌───────────────────────────────┐
│  Router                       │
│  role: acme_analyst           │
│  → loads acme.yaml            │
│  → scope check: PASS          │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  Alias resolver (code)        │
│  "orders" → metric.orders     │
│  "region" → dimension.region  │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  Binding resolver (code)      │
│  metric.orders →              │
│    table, value_col,          │
│    filter_predicates,         │
│    vintage guard, additivity  │
│  dimension.region →           │
│    table, key_col, join path  │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  LLM — Generator              │
│  receives: resolved bindings  │
│  produces: SQL + scope tag    │
└──────────┬────────────────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
┌─────────┐ ┌──────────────┐
│Reconciler│ │Parallel      │
│(code)    │ │Validator     │
│policy    │ │(LLM)         │
│checks    │ │semantic check│
└────┬─────┘ └──────┬───────┘
     │               │
     └──────┬─────────┘
            ▼
     Block / Retry / Pass
            │
            ▼
      Final response
```
