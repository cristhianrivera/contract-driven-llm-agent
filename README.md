# Contract-Driven LLM Agent Architecture

A production pattern for **multi-tenant analytics agents** where the agent's
behavior is governed by explicit, machine-readable contracts — not just prompts.

Named after Ramon Llull (1232–1316), the Catalan philosopher who built mechanical
reasoning systems from formal combinatorial rules. Same idea, different century.

---

## The Problem

Prompt-only agent control breaks in production in three predictable ways:

1. **Misinterpretation** — the model paraphrases your intent rather than executing it literally. "Always use the latest forecast" becomes "usually use the latest forecast."
2. **Context leakage** — in multi-tenant systems, a constraint defined for Customer A quietly influences Customer B's responses.
3. **No verification layer** — there is no post-generation check that the output actually respects the rules. The prompt is the only gate.

The pattern in this repo separates *specification* (what the agent must do) from
*verification* (proof that it did it), using two evaluation layers that most
production teams skip.

---

## Architecture Overview

```
User query
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  Layer 0 — Router                                   │
│  Scope check: is this query in-domain?              │
│  Context isolation: which tenant's ontology loads?  │
└────────────────────────┬────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Layer 1 — Generator (LLM)                          │
│  Ontology-grounded SQL / answer generation          │
│  Input: resolved metric/dimension/entity bindings   │
└────────────────────────┬────────────────────────────┘
                         │
              ┌──────────┴──────────┐
              │                     │
              ▼                     ▼
┌─────────────────────┐   ┌─────────────────────────┐
│  Layer 2a           │   │  Layer 2b               │
│  SQL Policy         │   │  Parallel Validator     │
│  Reconciler         │   │  (second LLM call)      │
│  (pure code)        │   │                         │
│  • forbidden joins  │   │  Re-reads the contract  │
│  • snapshot fences  │   │  independently and asks:│
│  • metric guards    │   │  "Is this answer true   │
│  • complexity caps  │   │   given the schema?"    │
└─────────┬───────────┘   └────────────┬────────────┘
          │                            │
          └──────────┬─────────────────┘
                     ▼
           Block / Retry / Pass
                     │
                     ▼
              Final response
```

### Key design decisions

**The Reconciler is pure code, not another LLM call.**
Rule evaluation (does this SQL contain a FULL OUTER JOIN? does it fence the
snapshot table?) is deterministic. Using an LLM for this check introduces the
same misinterpretation problem you're trying to solve. Code is the right tool.

**The Parallel Validator is a second LLM call — on purpose.**
Semantic correctness ("does this answer faithfully reflect what the metric
definition says?") is not a string-matching problem. A second model call, given
only the contract and the candidate answer, catches hallucinated column names and
wrong aggregation semantics that the Reconciler cannot see.

**`block_retry` injects a hint, not just a rejection.**
When a rule fires, the retry prompt includes a specific fix (`retry_hints` in
`sql_policy.yaml`). This avoids the failure loop where the model regenerates
the same violation because it doesn't know what was wrong.

**Context isolation is enforced at load time, not prompt time.**
Each tenant's ontology (`config/ontology/<domain>.yaml`) is loaded into an
isolated scope object. The router resolves which scope applies from the role
prefix and never merges scopes. Tenant A's metric definitions cannot leak into
Tenant B's query context regardless of prompt content.

---

## How the Ontology Works at Runtime

The architecture diagram shows the ontology as a box the Generator reads from.
This section explains what actually happens — because "ontology-grounded
generation" is easy to misread as "we put the schema in the prompt."

It is not that. Here is the difference.

**Without an ontology (schema-in-prompt approach):**

The LLM receives a CREATE TABLE dump and the user's question. It infers which
table to use, which columns to filter on, and whether the metric is additive.
It is usually right. When it is wrong, there is no systematic reason — the model
made a heuristic choice under uncertainty, and you cannot predict when it will
fail.

**With the ontology (this approach):**

Before the LLM is called, the system resolves the user's question against the
ontology in code:

1. **Alias resolution** — "orders" matches the `aliases` list for `metric.orders`.
   The concept is pinned to a canonical ID before the model sees anything.

2. **Binding resolution** — `metric.orders` declares exactly which table, which
   value column, and which filter predicates are required. These are injected
   into the prompt as constraints, not suggestions.

3. **Additivity check** — the metric's `additivity` field tells the system
   whether `SUM(valor)` is mathematically valid. The metric guard in
   `sql_policy.yaml` enforces this post-generation, in code.

4. **Relation resolution** — the join path between metric and dimension is
   declared in the ontology. The LLM does not infer join conditions from
   column name similarity.

The LLM's job is to assemble correct SQL from pre-resolved, structured inputs —
not to decide what those inputs should be.

**The consequence:** a question asked at temperature=0 should produce identical
SQL on every run. If it does not, the ontology binding is incomplete and the
model is filling gaps with heuristics. The `audit_determinism.md` prompt
exists specifically to surface this.

For a complete traced example — one question, every step — read
[`how_a_query_works.md`](how_a_query_works.md).

For the full map of what the agent always does, never does, and what each
ontology field controls — read [`agent_behaviour.md`](agent_behaviour.md).
This is the right starting point if you are implementing your own domain.

For a field-by-field explanation of what each YAML key does at runtime, read
[`ontology_reference.md`](ontology_reference.md).

---

## Repository Structure

```
how_a_query_works.md        ← start here: full end-to-end walkthrough of one query
agent_behaviour.md          ← what the agent always does, never does, and what you control
ontology_reference.md       ← field-by-field explanation of what each YAML key does

config/
  ontology/
    example_domain.yaml     ← template: metrics, dimensions, entities, bindings
  sql_policy.yaml           ← template: complexity rules, metric guards, retry hints
  tool_policies.yaml        ← per-role tool allowlists and cost ceilings

contracts/
  base_agent.yaml           ← core behavioral specification template
  business_rules.yaml       ← domain-agnostic business rule examples
  guardrails.yaml           ← pre/post generation guard templates

prompts/
  audit_contract_adherence.md   ← audit prompt: does the agent respect its spec?
  audit_context_leakage.md      ← audit prompt: does tenant context leak?
  audit_determinism.md          ← audit prompt: how stable are outputs across runs?

evaluation/
  model_metrics.py          ← Layer 1: standard ML eval (accuracy, latency, cost)
  system_metrics.py         ← Layer 2: contract compliance (the layer teams skip)
  audit_runner.py           ← runs both layers, produces a diagnostic report

test_cases/
  acme.json                 ← 15-case test suite for the acme example domain
```

**Real domain ontologies contain client-specific schemas and are not included.**
`example_domain.yaml` and `sql_policy.yaml` show the structure with a fictional
retail domain (`acme_corp`). Apply the same structure to your own schema.

---

## Two Evaluation Layers

Most teams measure the model. This repo measures the system.

| | Layer 1 — Model metrics | Layer 2 — System metrics |
|---|---|---|
| **What it measures** | Is the model accurate? Fast? Cheap? | Does the agent respect its contracts? |
| **Tools** | Standard ML eval frameworks | `system_metrics.py` in this repo |
| **Catches** | Hallucination, latency regressions | Context leakage, policy violations, schema drift |
| **When it fails** | Model gives wrong answer | Agent gives technically correct answer that breaks a business rule |
| **Who builds it** | Usually exists already | Usually missing entirely |

Layer 2 is where production failures live. A model can score 94% accuracy on
your eval set and still silently aggregate a non-additive metric, query a
snapshot table without a temporal fence, or apply Customer A's fiscal year
definition to Customer B's data.

---

## Audit Prompts

The `prompts/` directory contains three audit prompt templates used to
systematically test agent behavior before and after changes:

- **`audit_contract_adherence.md`** — feeds the agent a set of queries designed
  to stress-test each contract rule. Checks that block_retry fires on forbidden
  patterns and that metric guards prevent silent aggregation errors.

- **`audit_context_leakage.md`** — runs paired queries across two tenant
  contexts with deliberately similar phrasing. Verifies that Tenant A's
  dimension bindings do not appear in Tenant B's SQL output.

- **`audit_determinism.md`** — runs the same query N times and measures output
  variance. High variance on a semantically unambiguous query is a signal that
  the ontology binding is incomplete and the model is filling gaps with
  heuristics.

---

## The Multi-Tenant Problem

Running one agent for many customers against different schemas is the hardest
version of this problem. The naive approach — a single prompt with all customer
context concatenated — fails at scale because:

- Token budgets force truncation of older context
- The model interpolates between customer schemas when phrasing is similar
- There is no audit trail linking an answer to the specific schema version it used

The contract-driven approach solves this by making context explicit and isolated:
each customer's ontology is a versioned YAML file loaded into a named scope,
and every generated query is tagged with the scope version it used. Debugging
a wrong answer means checking one file, not a 40k-token prompt.

---

## Getting Started

1. Copy `config/ontology/example_domain.yaml` → `config/ontology/<your_domain>.yaml`
2. Replace all `acme` references with your domain name and role prefix
3. Map your schema's tables, columns, and metrics to the binding structure
4. Copy `config/sql_policy.yaml` and update the `role_prefix` and `data_domain`
5. Run `evaluation/audit_runner.py` against your domain to baseline behavior

---

## What This Repo Does and Does Not Include

**Included:** the contracts, ontology template, SQL policy, reconciler checks
(AST-based, via sqlglot), audit prompts, and both evaluation layers. These
are the harness — the part that makes the generator's output verifiable.

**Not included:** the alias resolver, binding resolver, and generator/retry loop.
These are the parts that actually call the LLM, inject resolved bindings into
the prompt, and handle retries when the Reconciler fires. They are the
proprietary core of the internal deployment and are not open-sourced here.

The interface those components must satisfy is straightforward:

```python
def agent_fn(query: str, domain: str) -> tuple[str, dict]:
    """
    Args:
        query:  natural-language question from the user
        domain: scope name (e.g. "acme") — determines which ontology file loads

    Returns:
        sql:    generated SQL string, or empty string if the query was declined
        usage:  dict with keys prompt_tokens, completion_tokens, cost_usd
    """
```

Plug any implementation of this interface into `audit_runner.py` and the full
evaluation harness runs against it.

**Known gaps worth flagging for production use:**

*Router mechanism.* The architecture describes scope scoring (threshold: 0.80)
and alias resolution, but does not specify whether these use exact string
matching, embedding cosine similarity, or a classifier. Exact match is fragile
against real-world phrasing variance. The internal deployment uses an embedding-
based approach; the mechanism is part of the proprietary core not open-sourced
here. If you implement your own, test it with `audit_determinism.md` — variance
across runs at temperature=0 usually traces back to alias resolution, not the
generator.

*Retry exhaustion.* `guardrails.yaml` caps retries at 2. The harness does not
specify what the user-facing response looks like when retries are exhausted
versus a clean decline (which produces a different, more informative message).
In the internal deployment these are handled separately; implement them
differently in your UX.

*Online monitoring.* Everything here is pre-deployment audit. There is no
mechanism for continuous production monitoring — e.g. sampling live traffic and
re-running `system_metrics.py` checks to detect contract compliance drift over
time. This is a real gap for production deployments. The offline audit suite
is a necessary baseline, not a substitute for online observability.

*Ontology YAML validation.* The Reconciler and Parallel Validator are only as
good as the ontology they read. There is no automated check that the ontology
file itself is internally consistent or matches the live database schema. Treat
ontology changes like schema migrations: reviewed, version-controlled, and run
through the contract adherence audit before deployment.

---

## Cost and Latency of Two LLM Calls

The architecture runs two LLM calls per query: the Generator (Layer 1) and the
Parallel Validator (Layer 2b). The questions worth asking before adopting this:

**Does the second call double cost?** Not quite. The Parallel Validator receives
a much smaller prompt than the Generator — only the generated SQL and the
relevant ontology excerpt, not the full schema context. In practice the second
call runs at roughly 30–40% of the Generator's token count. Total cost per query
is closer to 1.3–1.4× a single call, not 2×.

**Does it double latency?** No — the Reconciler (pure code, <1ms) and the
Parallel Validator run in parallel after generation. End-to-end latency is
Generator latency + Parallel Validator latency, where Validator latency is
typically shorter. The critical path is the Generator call.

**When is the second call worth it?** When a wrong answer is worse than a slow
one — typically in any enterprise analytics context where a silent aggregation
error or leaked tenant data causes a business decision to be made on incorrect
numbers. The cost of the Parallel Validator is the insurance premium; the value
is catching the failure before it reaches the user rather than after.

**Ontology YAML as ground truth.** The Parallel Validator is only as good as
the ontology it reads. If the ontology says a metric is additive when it is not,
the Validator will pass queries that are mathematically wrong. Ontology files
should be treated like schema migrations: reviewed by a domain expert, version-
controlled, and tested with the `audit_contract_adherence.md` suite before any
change is deployed. There is no automated ontology validator in this repo —
that is a gap worth filling for production use.

---

## Background

This pattern is the conclusion of a process that started with the wrong framing.
The wrong framing was not "we need a schema in the prompt." We had that from
day one. The wrong framing was treating every failure as a model quality problem.

**What the prompt already contained.** The generator received a filtered subset
of the database schema — not a raw dump, but only the tables the orchestrator
had determined were relevant to the query. On top of that: business rules as
text, entity definitions, categorical values retrieved by FAISS similarity
search, few-shot SQL examples retrieved by FAISS similarity to the user query,
temporal rules, and database-specific guidelines. The static portions were
prompt-cached at the role level for latency and cost efficiency. This is a
serious prompt architecture, not a naive schema dump.

The model read all of it. It understood it. And it still occasionally got
things wrong in a specific class of ways that no amount of prompt improvement
was fixing.

Here is the sequence of things we tried on top of that baseline:

**Better few-shot examples.** We curated and expanded the Q→SQL pairs
injected into the prompt. Accuracy improved modestly, then plateaued. The
model was still free to disregard the examples when phrasing varied enough.

**FAISS retrieval over validated SQL.** We replaced static examples with
semantic search over a validated SQL library — retrieve the most similar past
query, use it as a reference. This worked for high-frequency phrasings and
failed silently on variations the retrieval didn't recognise as similar. The
threshold tuning became its own maintenance burden.

**SQL templates with slot-filling.** We abstracted validated queries into
parameterised templates and had the model fill in the slots. Tested in
production against the previous approach, it lost ground — around 2500bps
on the metrics we were tracking. The model was unreliable at extracting slot
values from natural language, and template coverage was always incomplete.

**Fine-tuning embeddings and rerankers.** We explored fine-tuning the
similarity layer (NLI rerankers, higher-capacity embedding models) to improve
retrieval precision. Each iteration required labelled data, compute, and
evaluation cycles — and addressed retrieval quality, not generation correctness.

**The reframe.** After enough of these cycles, the pattern became clear. The
problem was not what the model knew. The business rules were in the prompt. The
entity definitions were in the prompt. The model could read that occupancy is
non-additive, that forecast views require a vintage filter, that snapshot tables
need a temporal fence. It knew. The problem was what happened when it didn't
apply what it knew — which was silent, unpredictable, and had no systematic
catch.

A rule written in a prompt is a suggestion the model can miss. A post-generation
AST check that blocks any query violating that rule is a guarantee. The ontology
does not replace the business rules and entity definitions already in the prompt
— it makes them machine-enforceable rather than model-readable. Specifically:

- Alias resolution moves from "FAISS retrieves the closest example" to
  "deterministic lookup pins the concept before the LLM is called"
- Filter predicates move from "rule the model should follow" to "constraint
  injected as a required input"
- Additivity moves from "guideline in the prompt" to "guard that runs in code
  post-generation and blocks violations regardless of what the model decided"

The contracts, reconciler, and parallel validator were added one by one as
specific production failure modes appeared — non-additive metrics being SUMmed
silently, snapshot tables returning duplicates, tenant context leaking across
sessions. The audit prompts were written to reproduce each failure before the
fix and confirm it didn't regress after.

**Measured results.** We ran a controlled evaluation across four conditions on
the same isolated question set (one session per question, verified against
expected SQL and expected answer):

| Architecture | SQL examples | Success rate |
|---|---|---|
| Prompt-based (baseline) | None | 65% |
| Prompt-based (baseline) | 200+ hand-maintained, FAISS-retrieved | 87% |
| Contract architecture | 200+ hand-maintained, FAISS-retrieved | 91% |
| Contract architecture | None | **93%** |

Three things are worth naming in this table:

First, the contract architecture alone (93%) outperforms the best prompt-based
approach with 200+ curated SQL examples (87%) by 6 points. The enforcement
layer is doing more work than the examples were.

Second, contracts alone (93%) beats contracts with examples (91%). This is not
a surprising result — it is a direct consequence of the architecture. Once alias
resolution is deterministic and filter predicates are injected as constraints,
a retrieved SQL example can only introduce noise: it brings a phrasing pattern
and table reference that may conflict with what the binding already resolved
correctly. The model receives two signals pulling in potentially different
directions, and the weaker one occasionally wins. In a prompt-based system,
examples are the primary resolution mechanism. In a contract-based system, they
are redundant at best and contradictory at worst.

Third, the cost structure inverts. The 87% result requires ongoing curation of
a SQL example library — every schema change, every new query pattern, every new
role is a maintenance event. The 93% result requires maintaining the ontology
YAML — more structured, more explicit, and failures are loud rather than silent
when it drifts.

The result is a system that fails less often and fails loudly when it does.
That is a better engineering property than a system that scores higher on an
eval set but fails unpredictably in production.

If you are building a Text-to-SQL system and already have a solid prompt with
schema, rules, and retrieved examples: this pattern is the next layer. The
question is not "how do I make the model better at applying the rules?" but
"which rules does the model not need to apply at all because the system
enforces them in code?"
---

## Origin

The name comes from Ramon Llull (1232–1316), the Catalan philosopher who built
mechanical reasoning systems from formal combinatorial rules. Same idea,
different century.

This pattern emerged from a production multi-tenant analytics deployment. It is
published here as a standalone, domain-agnostic template because the core
problem — making LLM agents reliable at the system level, not just the model
level — is not specific to any one product.

---

## License

MIT. Use freely. Attribution appreciated but not required.
