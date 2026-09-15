"""
evaluation/audit_runner.py

Runs both evaluation layers (model metrics + system metrics) against a test
suite and produces a combined diagnostic report.

Usage:
    python evaluation/audit_runner.py --domain acme --suite test_cases/acme.json
    python evaluation/audit_runner.py --domain acme --suite test_cases/acme.json --output-dir evaluation/reports/
"""

import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

from model_metrics import run_model_metrics
from system_metrics import run_system_metrics


PASS_THRESHOLDS = {
    # Layer 1
    # exact_match_rate is intentionally excluded from the pass gate: it fails on
    # formatting differences between semantically identical queries. Use predicate
    # coverage as the primary accuracy signal.
    "avg_predicate_coverage": 0.90,
    "decline_accuracy": 1.00,
    "avg_latency_ms": 3000,
    # Layer 2
    # contract_compliance_rate is gated at 1.00 because the checkers are structural
    # (AST-based), not probabilistic. A violation that passes the checker is a
    # checker bug, not a tolerable failure — so the threshold stays at 1.00, but
    # confidence in the threshold depends on checker coverage, not just the rate.
    "contract_compliance_rate": 1.00,
    "scope_tag_accuracy": 1.00,
}


def format_check(label: str, value: float, threshold: float, is_latency: bool = False) -> str:
    if is_latency:
        ok = value <= threshold
        return f"  {'✓' if ok else '✗'} {label}: {value:.0f}ms (threshold ≤ {threshold:.0f}ms)"
    ok = value >= threshold
    return f"  {'✓' if ok else '✗'} {label}: {value:.1%} (threshold ≥ {threshold:.1%})"


def run_audit(
    domain: str,
    suite_path: str,
    agent_fn,
    snapshot_tables: list = None,
    non_additive_metrics: list = None,
    output_dir: str = "evaluation/reports",
    using_stub_agent: bool = False,
) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  contract-agent audit — domain: {domain}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    if using_stub_agent:
        print("NOTE: no agent_fn supplied — running against mock_agent_fn, a stub that")
        print("      returns one fixed SQL string for every query. A FAIL verdict below")
        print("      is expected and says nothing about the harness. Wire in a real")
        print("      agent_fn (see README) to get meaningful Layer 1 numbers.\n")

    # ── Layer 1 ────────────────────────────────────────────────────────────────
    print("LAYER 1 — Model Metrics")
    print("-" * 40)
    l1_output = f"{output_dir}/layer1_{domain}_{ts}.json"
    l1 = run_model_metrics(domain, suite_path, agent_fn, l1_output)

    print(format_check("Predicate coverage",     l1["avg_predicate_coverage"], PASS_THRESHOLDS["avg_predicate_coverage"]))
    print(format_check("Decline accuracy",       l1["decline_accuracy"],       PASS_THRESHOLDS["decline_accuracy"]))
    print(format_check("Avg latency",            l1["avg_latency_ms"],         PASS_THRESHOLDS["avg_latency_ms"], is_latency=True))
    print(f"  Total cost: ${l1['total_cost_usd']:.4f}")
    print(f"  Exact match rate: {l1['exact_match_rate']:.1%}  (informational — not a pass/fail gate)")

    # ── Layer 2 ────────────────────────────────────────────────────────────────
    print("\nLAYER 2 — Contract Compliance")
    print("-" * 40)
    l2_output = f"{output_dir}/layer2_{domain}_{ts}.json"
    l2 = run_system_metrics(domain, suite_path, agent_fn, snapshot_tables, non_additive_metrics, l2_output)

    print(format_check("Contract compliance",  l2["contract_compliance_rate"], PASS_THRESHOLDS["contract_compliance_rate"]))
    print(format_check("Scope tag accuracy",   l2["scope_tag_accuracy"],       PASS_THRESHOLDS["scope_tag_accuracy"]))
    print(f"  Blocked: {l2['blocked']} / {l2['total_cases']}")

    if l2["violations_by_rule"]:
        print("  Violations by rule:")
        for rule, count in l2["violations_by_rule"].items():
            print(f"    {rule}: {count}")

    # ── Overall verdict ────────────────────────────────────────────────────────
    l1_pass = (
        l1["avg_predicate_coverage"] >= PASS_THRESHOLDS["avg_predicate_coverage"]
        and l1["decline_accuracy"]   >= PASS_THRESHOLDS["decline_accuracy"]
        and l1["avg_latency_ms"]     <= PASS_THRESHOLDS["avg_latency_ms"]
    )
    l2_pass = (
        l2["contract_compliance_rate"] >= PASS_THRESHOLDS["contract_compliance_rate"]
        and l2["scope_tag_accuracy"]   >= PASS_THRESHOLDS["scope_tag_accuracy"]
    )

    overall = "PASS" if (l1_pass and l2_pass) else "FAIL"
    print(f"\n{'='*60}")
    print(f"  OVERALL: {overall}")
    print(f"  Layer 1 (model):    {'PASS' if l1_pass else 'FAIL'}")
    print(f"  Layer 2 (contract): {'PASS' if l2_pass else 'FAIL'}")
    if using_stub_agent:
        print("  (stub agent — Layer 1 FAIL is expected; see note above)")
    print(f"{'='*60}\n")

    report = {
        "domain": domain,
        "timestamp": ts,
        "overall": overall,
        "layer1": {"status": "PASS" if l1_pass else "FAIL", **l1},
        "layer2": {"status": "PASS" if l2_pass else "FAIL", **l2},
    }

    report_path = f"{output_dir}/report_{domain}_{ts}.json"
    Path(report_path).write_text(json.dumps(report, indent=2))
    print(f"Full report: {report_path}")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="contract-agent audit runner — both evaluation layers")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--suite", required=True, help="Path to test suite JSON")
    parser.add_argument("--output-dir", default="evaluation/reports")
    parser.add_argument("--snapshot-tables", nargs="*", default=["sales.contracts"])
    parser.add_argument("--non-additive-metrics", nargs="*", default=["contracts_active", "occupancy"])
    args = parser.parse_args()

    # Stub: replace with your agent callable.
    # This exists only so the script runs standalone — it does not represent
    # a real agent. See README for the interface contract agent_fn must satisfy.
    def mock_agent_fn(query: str, domain: str):
        return (
            f"-- scope: {domain} | role: {domain}_analyst | ontology_version: 1\n"
            f"SELECT region, SUM(valor) FROM sales.orders_v WHERE order_date >= '2026-01-01' GROUP BY region",
            {"prompt_tokens": 150, "completion_tokens": 60, "cost_usd": 0.0015},
        )

    run_audit(
        domain=args.domain,
        suite_path=args.suite,
        agent_fn=mock_agent_fn,
        snapshot_tables=args.snapshot_tables,
        non_additive_metrics=args.non_additive_metrics,
        output_dir=args.output_dir,
        using_stub_agent=True,
    )
