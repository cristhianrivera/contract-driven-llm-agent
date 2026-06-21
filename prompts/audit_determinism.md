# Audit Prompt: Output Determinism

Use this prompt to measure how stable agent outputs are across repeated runs
of the same query. High variance on semantically unambiguous queries signals
incomplete ontology binding — the model is filling gaps with heuristics.

---

## Setup

Set LLM temperature to 0 for this audit. Variance at temperature=0 indicates
structural ambiguity in the ontology or prompt, not random sampling.

Run each test query **5 times** in independent sessions (clear context between runs).
Record the generated SQL for each run.

---

## Test Cases

### DT001 — Canonical metric query

**Query:**
> What were total orders by region in January 2026?

**Expected:** Identical SQL across all 5 runs.

**Variance signals:**
- Different table names across runs → metric binding is ambiguous
- Different filter predicates → `filter_predicates` not fully constraining
- Different GROUP BY columns → dimension binding resolves to different columns

---

### DT002 — Alias resolution

**Query:**
> Show me confirmed orders last month.

("confirmed orders" is an alias for `metric.orders`)

**Expected:** All 5 runs resolve to the same canonical SQL.

**Variance signals:**
- Some runs use `indicador = 'orders'`, others use a different predicate
- Some runs join to the region table, others do not

---

### DT003 — Time range default

**Query:**
> Show me revenue this year.

(No explicit date range — agent must apply `temporal_defaults.default_period`)

**Expected:** All 5 runs use the same date range (e.g. `DATE_TRUNC('year', CURRENT_DATE)` to `CURRENT_DATE`).

**Variance signals:**
- Some runs use calendar year, others use fiscal year
- Some runs use `CURRENT_DATE`, others hardcode a date

---

### DT004 — Ambiguous query (intentional)

**Query:**
> Show me cancellations.

(No time range, no dimension — intentionally underspecified)

**Expected:** All 5 runs ask the same clarifying question (not generate SQL).

**Variance signals:**
- Some runs ask for time range, others ask for region, others generate SQL without asking
- Inconsistent clarifying questions signal the ambiguity_strategy is not enforced

---

## Variance Measurement

For each test, compute:

```
SQL diff count = number of runs where SQL differs from run 1
Structural variance = number of unique SQL ASTs across 5 runs
```

A query with 0 structural variance across 5 runs at temperature=0 is fully
bound. A query with 3+ unique ASTs needs ontology review.

---

## Scoring

| Test | Runs | Unique SQLs | Variance | Action |
|---|---|---|---|---|
| DT001 canonical | 5 | | | |
| DT002 alias | 5 | | | |
| DT003 time default | 5 | | | |
| DT004 ambiguous | 5 | | | |

**Threshold:**
- DT001, DT002, DT003: ≤ 1 unique SQL (fully deterministic)
- DT004: all 5 runs must ask (not generate SQL); clarifying question may vary in wording

Any metric scoring > 1 unique SQL at temperature=0 must be diagnosed before
shipping. Check: missing filter_predicates, ambiguous alias mappings, or
underspecified temporal defaults.
