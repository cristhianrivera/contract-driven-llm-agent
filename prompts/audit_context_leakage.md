# Audit Prompt: Context Leakage (Multi-Tenant)

Use this prompt to verify that tenant A's ontology does not influence
tenant B's query context. Run after any change to the router, scope loader,
or ontology files.

---

## Setup

You need two tenant configurations loaded side by side:
- **Tenant A:** `acme` domain — metrics include `metric.orders`, `metric.revenue`
- **Tenant B:** a second domain with different metric names and table bindings

If you only have one domain configured, create a minimal second domain YAML
(`config/ontology/test_domain.yaml`) with one metric that uses a different
table name. This is sufficient to detect leakage.

---

## Test Cases

### LK001 — Table name isolation

Run the following query in **Tenant A** session:
> Show me orders by region for January 2026.

Then run the **identical query** in a **Tenant B** session.

**Expected behavior:**
- Tenant A SQL references `sales.orders_v` (or the acme binding)
- Tenant B SQL references Tenant B's declared table, NOT `sales.orders_v`

**Failure signal:**
- Tenant B's SQL references any table declared only in Tenant A's ontology
- Tenant B's SQL uses `cod_region` (an acme column) when Tenant B's schema uses a different key

---

### LK002 — Metric alias isolation

Tenant A has an alias `"confirmed orders"` → `metric.orders`.
Tenant B does not declare this alias.

Run in **Tenant B** session:
> How many confirmed orders did we have last month?

**Expected behavior:**
- Agent says it does not recognize "confirmed orders" as a known metric in this context
- OR agent asks for clarification

**Failure signal:**
- Agent resolves "confirmed orders" to `metric.orders` using Tenant A's alias table
- SQL references Tenant A's tables

---

### LK003 — Filter predicate isolation

Tenant A's `metric.orders` requires `indicador = 'orders'` as a filter predicate.
Tenant B's equivalent metric uses `type = 'confirmed'`.

Run in **Tenant B** session:
> Show me total orders for this month.

**Expected behavior:**
- SQL contains `type = 'confirmed'` (Tenant B's predicate)
- SQL does NOT contain `indicador = 'orders'` (Tenant A's predicate)

**Failure signal:**
- Tenant B's SQL includes `indicador = 'orders'` — Tenant A's filter leaked

---

### LK004 — Scope header in generated SQL

Every generated SQL should include a comment header identifying the scope:

```sql
-- scope: acme | role: acme_analyst | ontology_version: 1
```

**Expected behavior:**
- Tenant A queries are tagged with `scope: acme`
- Tenant B queries are tagged with `scope: <tenant_b_domain>`

**Failure signal:**
- Tenant B's SQL is tagged with `scope: acme`
- Scope comment is absent entirely

---

## Scoring

| Test | PASS | FAIL | Notes |
|---|---|---|---|
| LK001 table isolation | | | |
| LK002 alias isolation | | | |
| LK003 predicate isolation | | | |
| LK004 scope header | | | |

**Threshold:** All 4 must PASS. Any leakage failure is a P0 — do not ship.
