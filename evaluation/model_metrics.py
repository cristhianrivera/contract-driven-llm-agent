"""
evaluation/model_metrics.py

Layer 1 evaluation: standard ML metrics for the analytics agent.
Measures accuracy, latency, and cost — the metrics most teams already track.

Layer 1 alone is insufficient. A system can score well here and still silently
violate business rules — summing a non-additive metric, omitting a mandatory
filter predicate, querying a snapshot table without a temporal fence. Those
failures don't show up as wrong answers in an accuracy eval; they show up as
plausible-looking numbers that are quietly incorrect. Layer 2 (system_metrics.py)
exists specifically to catch what Layer 1 misses.

Key design choice: avg_predicate_coverage is the primary accuracy signal here,
not exact_match_rate. Predicate coverage measures whether mandatory filter
constraints — the ones the ontology requires — are present in the output.
Exact match fails on formatting differences between semantically identical
queries and is not used as a pass/fail gate.

Usage:
    python evaluation/model_metrics.py --domain acme --suite test_cases/acme.json
"""

import json
import time
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path


@dataclass
class TestCase:
    id: str
    query: str
    expected_sql: Optional[str] = None          # exact SQL match (optional)
    expected_tables: list[str] = field(default_factory=list)   # tables that must appear
    expected_columns: list[str] = field(default_factory=list)  # columns that must appear
    expected_predicates: list[str] = field(default_factory=list)  # WHERE clauses that must appear
    should_decline: bool = False                # True if the query should be declined


@dataclass
class ModelMetricsResult:
    test_id: str
    query: str
    generated_sql: Optional[str]
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    # Accuracy dimensions
    exact_match: bool = False
    table_coverage: float = 0.0     # fraction of expected_tables present
    column_coverage: float = 0.0    # fraction of expected_columns present
    predicate_coverage: float = 0.0 # fraction of expected_predicates present
    declined_correctly: bool = False
    error: Optional[str] = None


def evaluate_sql_coverage(generated: str, expected_items: list[str]) -> float:
    """Fraction of expected items (table/column/predicate strings) present in generated SQL."""
    if not expected_items:
        return 1.0
    generated_lower = generated.lower()
    hits = sum(1 for item in expected_items if item.lower() in generated_lower)
    return hits / len(expected_items)


def run_model_metrics(
    domain: str,
    test_suite_path: str,
    agent_fn,           # callable(query: str, domain: str) -> (sql: str, usage: dict)
    output_path: Optional[str] = None,
) -> dict:
    """
    Run Layer 1 evaluation against a test suite JSON file.

    Args:
        domain: domain name (e.g. "acme")
        test_suite_path: path to JSON file containing list of TestCase dicts
        agent_fn: callable that takes (query, domain) and returns (sql_str, usage_dict)
                  usage_dict should have keys: prompt_tokens, completion_tokens, cost_usd
        output_path: if set, write results JSON here

    Returns:
        summary dict with aggregate metrics
    """
    test_cases = [TestCase(**tc) for tc in json.loads(Path(test_suite_path).read_text())]
    results = []

    for tc in test_cases:
        start = time.perf_counter()
        try:
            sql, usage = agent_fn(tc.query, domain)
            latency_ms = (time.perf_counter() - start) * 1000
            declined = sql is None or sql.strip() == ""

            result = ModelMetricsResult(
                test_id=tc.id,
                query=tc.query,
                generated_sql=sql,
                latency_ms=latency_ms,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                cost_usd=usage.get("cost_usd", 0.0),
                declined_correctly=(declined == tc.should_decline),
            )

            if not declined and not tc.should_decline:
                result.exact_match = (sql.strip() == (tc.expected_sql or "").strip())
                result.table_coverage = evaluate_sql_coverage(sql, tc.expected_tables)
                result.column_coverage = evaluate_sql_coverage(sql, tc.expected_columns)
                result.predicate_coverage = evaluate_sql_coverage(sql, tc.expected_predicates)

        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            result = ModelMetricsResult(
                test_id=tc.id,
                query=tc.query,
                generated_sql=None,
                latency_ms=latency_ms,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                error=str(e),
            )

        results.append(result)

    # Aggregate
    n = len(results)
    n_valid = sum(1 for r in results if r.error is None)
    summary = {
        "domain": domain,
        "total_cases": n,
        "errors": n - n_valid,
        "avg_latency_ms": sum(r.latency_ms for r in results) / n if n else 0,
        "p95_latency_ms": sorted(r.latency_ms for r in results)[int(n * 0.95)] if n else 0,
        "total_cost_usd": sum(r.cost_usd for r in results),
        # Coverage metrics are the primary accuracy signal.
        # avg_predicate_coverage is the most meaningful: it measures whether mandatory
        # filter predicates (the ones the ontology requires) are present in the output.
        "avg_predicate_coverage": sum(r.predicate_coverage for r in results) / n_valid if n_valid else 0,
        "avg_table_coverage": sum(r.table_coverage for r in results) / n_valid if n_valid else 0,
        "avg_column_coverage": sum(r.column_coverage for r in results) / n_valid if n_valid else 0,
        # exact_match_rate is a weak signal for SQL: formatting differences fail it even
        # when two queries are semantically identical. Reported for completeness; do not
        # use as a pass/fail gate. Use predicate_coverage instead.
        "exact_match_rate": sum(r.exact_match for r in results) / n_valid if n_valid else 0,
        "decline_accuracy": sum(r.declined_correctly for r in results) / n if n else 0,
        "results": [asdict(r) for r in results],
    }

    if output_path:
        Path(output_path).write_text(json.dumps(summary, indent=2))
        print(f"Layer 1 results written to {output_path}")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Layer 1: Model metrics evaluation")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--suite", required=True, help="Path to test suite JSON")
    parser.add_argument("--output", default="evaluation/results_model_metrics.json")
    args = parser.parse_args()

    # Stub: replace with your agent callable.
    # This exists only so the script runs standalone — it does not represent
    # a real agent. See README for the interface contract agent_fn must satisfy.
    def mock_agent_fn(query: str, domain: str):
        return ("SELECT 1", {"prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.001})

    summary = run_model_metrics(args.domain, args.suite, mock_agent_fn, args.output)
    print(f"Avg predicate coverage:{summary['avg_predicate_coverage']:.1%}")
    print(f"Avg table coverage:    {summary['avg_table_coverage']:.1%}")
    print(f"Avg column coverage:   {summary['avg_column_coverage']:.1%}")
    print(f"Decline accuracy:      {summary['decline_accuracy']:.1%}")
    print(f"Avg latency:           {summary['avg_latency_ms']:.0f}ms")
    print(f"Total cost:            ${summary['total_cost_usd']:.4f}")
    print(f"Exact match rate:      {summary['exact_match_rate']:.1%}  (weak signal — see note in source)")
