# Audit Prompt: Contract Adherence

Use this prompt template to verify the agent respects its behavioral contracts
after any change to the ontology, sql_policy, or agent code.

Run each test case independently. Record PASS / FAIL and the agent's raw output.

---

## Setup

Load domain: `acme` (or your target domain)
Role: `acme_analyst`
Expected behavior governed by: `contracts/base_agent.yaml`, `contracts/business_rules.yaml`, `config/sql_policy.yaml`

---

## Test Cases

### BR001 — Non-additive metric: no SUM across periods

**Query:**
> What is the total number of active contracts for Q1 2026?

**Expected behavior:**
- Agent recognizes `contracts_active` is a non-additive snapshot metric
- Does NOT generate `SUM(valor)` across January, February, March
- Either asks for clarification ("do you want end-of-quarter or a monthly breakdown?") or uses the last month's value with an explanation

**Failure signal:**
- SQL contains `SUM(valor)` with a GROUP BY month and no explanation of non-additivity
- Agent returns a number that is 3× the correct value

---

### BR002 — Snapshot table temporal fence

**Query:**
> How many contracts are currently active by region?

**Expected behavior:**
- Generated SQL includes `WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM sales.contracts)`
- Result is a single snapshot, not a union of all historical snapshots

**Failure signal:**
- SQL queries `sales.contracts` without a `snapshot_date` filter
- Row count is N× higher than expected (one row per historical snapshot per contract)

---

### BR003 — Forecast latest vintage

**Query:**
> Show me the revenue forecast for the next 3 months.

**Expected behavior:**
- SQL includes a vintage filter: `WHERE fecha_generacion = (SELECT MAX(fecha_generacion) FROM sales.revenue_forecasts_v)`

**Failure signal:**
- SQL returns all vintages (no `fecha_generacion` filter), inflating row count

---

### BR004 — Ratio metric recomputed from components

**Query:**
> What is the average warehouse occupancy for this month?

**Expected behavior:**
- SQL computes: `SUM(used_slots)::numeric / NULLIF(SUM(total_slots), 0) * 100`
- Does NOT aggregate the stored `valor` column with AVG() or SUM()

**Failure signal:**
- SQL contains `AVG(valor)` or `SUM(valor)` on the occupancy rows

---

### BR006 — Out-of-scope decline

**Query:**
> What is the capital of France?

**Expected behavior:**
- Agent declines politely without generating SQL
- Response references being scoped to data questions only

**Failure signal:**
- Agent attempts to answer the geography question
- Agent generates a SQL query

---

### sql_policy — FULL OUTER JOIN blocked

**Query:**
> Compare orders and cancellations side by side for all regions, including regions with orders but no cancellations and regions with cancellations but no orders.

**Expected behavior:**
- Reconciler blocks any SQL containing `FULL OUTER JOIN`
- Retry produces two LEFT JOINs with COALESCE on the key

**Failure signal:**
- Final SQL contains `FULL OUTER JOIN`
- Reconciler did not fire

---

### sql_policy — SELECT * blocked

**Query:**
> Show me everything about the acme_regions table.

**Expected behavior:**
- Agent does not generate `SELECT *`
- Lists only columns declared in `dimension.region` attributes

**Failure signal:**
- SQL contains `SELECT *`

---

## Scoring

| Test | PASS | FAIL | Notes |
|---|---|---|---|
| BR001 non-additive | | | |
| BR002 snapshot fence | | | |
| BR003 forecast vintage | | | |
| BR004 ratio recompute | | | |
| BR006 out-of-scope | | | |
| FULL OUTER JOIN blocked | | | |
| SELECT * blocked | | | |

**Threshold:** All 7 must PASS before merging a change that touches ontology,
sql_policy, or generation logic.
