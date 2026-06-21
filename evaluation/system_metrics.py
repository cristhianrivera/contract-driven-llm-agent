"""
evaluation/system_metrics.py

Layer 2 evaluation: contract compliance metrics.
Measures whether the agent respects its behavioral contracts — the layer
most teams skip and where production failures actually live.

Rule checkers use sqlglot AST traversal rather than regex on raw SQL text.
This matters: regex on SQL strings false-positives on string literals, comments,
and whitespace variants. AST checks operate on the parsed structure, so
  SELECT '-- FULL OUTER JOIN note' AS x
does not trigger the full-outer-join check, and
  SELECT  *  FROM t
(extra whitespace) does not escape the star-select check.

Requires: sqlglot >= 20.0  (pip install sqlglot)

Usage:
    python evaluation/system_metrics.py --domain acme --suite test_cases/acme.json
"""

import json
import re
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp


@dataclass
class ContractViolation:
    rule_id: str
    rule_name: str
    description: str
    severity: str   # "block" | "warn"


@dataclass
class SystemMetricsResult:
    test_id: str
    query: str
    generated_sql: Optional[str]
    violations: list[ContractViolation] = field(default_factory=list)
    scope_tag_present: bool = False
    scope_tag_correct: bool = False
    retry_count: int = 0
    final_status: str = "pass"   # "pass" | "blocked" | "warned" | "error"
    parse_error: bool = False    # True if sqlglot could not parse the SQL
    error: Optional[str] = None


# ── AST helpers ────────────────────────────────────────────────────────────────

def _parse(sql: str) -> Optional[exp.Expression]:
    """
    Parse SQL with sqlglot. Returns None if parsing fails and records that
    fact so callers can fall back to a conservative violation rather than
    silently passing unparseable SQL.
    """
    try:
        return sqlglot.parse_one(sql, error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.SqlglotError:
        return None


# ── Rule checkers (AST-based) ──────────────────────────────────────────────────

def check_full_outer_join(tree: Optional[exp.Expression]) -> Optional[ContractViolation]:
    """
    Block FULL OUTER JOIN at the AST level.
    Walks all Join nodes and checks join_type, so string literals or comments
    containing the phrase do not trigger a false positive.
    """
    if tree is None:
        # Unparseable SQL: conservative block — we cannot verify it is clean.
        return ContractViolation(
            rule_id="sql_policy.forbidden_full_outer_join",
            rule_name="full_outer_join_blocked",
            description="SQL could not be parsed; cannot verify absence of FULL OUTER JOIN.",
            severity="block",
        )
    for join in tree.find_all(exp.Join):
        if join.args.get("kind", "").upper() == "FULL":
            return ContractViolation(
                rule_id="sql_policy.forbidden_full_outer_join",
                rule_name="full_outer_join_blocked",
                description="SQL contains FULL OUTER JOIN — rewrite as two LEFT JOINs.",
                severity="block",
            )
    return None


def check_union_all(tree: Optional[exp.Expression]) -> Optional[ContractViolation]:
    """
    Block UNION ALL at the AST level.
    Detects Union nodes where the 'distinct' flag is False (i.e. UNION ALL).
    """
    if tree is None:
        return ContractViolation(
            rule_id="sql_policy.forbidden_union_all",
            rule_name="union_all_blocked",
            description="SQL could not be parsed; cannot verify absence of UNION ALL.",
            severity="block",
        )
    for union in tree.find_all(exp.Union):
        if not union.args.get("distinct", True):
            return ContractViolation(
                rule_id="sql_policy.forbidden_union_all",
                rule_name="union_all_blocked",
                description="SQL contains UNION ALL — use a single fact view with an indicador filter.",
                severity="block",
            )
    return None


def check_star_select(tree: Optional[exp.Expression]) -> Optional[ContractViolation]:
    """
    Block SELECT * at the AST level.
    Finds Star expression nodes in Select projections.
    SELECT t.* is also blocked — both are contract violations.
    """
    if tree is None:
        return ContractViolation(
            rule_id="sql_policy.forbidden_star_select",
            rule_name="star_select_blocked",
            description="SQL could not be parsed; cannot verify absence of SELECT *.",
            severity="block",
        )
    for select in tree.find_all(exp.Select):
        for expr in select.expressions:
            # bare * → Star; table.* → Column wrapping a Star
            if isinstance(expr, exp.Star):
                return ContractViolation(
                    rule_id="sql_policy.forbidden_star_select",
                    rule_name="star_select_blocked",
                    description="SQL contains SELECT * — list columns explicitly.",
                    severity="block",
                )
            if isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
                return ContractViolation(
                    rule_id="sql_policy.forbidden_star_select",
                    rule_name="star_select_blocked",
                    description="SQL contains SELECT <table>.* — list columns explicitly.",
                    severity="block",
                )
    return None


def check_snapshot_fence(
    tree: Optional[exp.Expression],
    sql: str,
    snapshot_tables: list[str],
) -> Optional[ContractViolation]:
    """
    Block snapshot table queries that lack a temporal fence.

    Table reference detection uses the AST; the fence predicate check falls back
    to a targeted string search on the WHERE clause text. Full predicate-structure
    checking via AST is possible but adds complexity that outweighs the benefit
    for this check — the false-positive risk here is low because snapshot_date is
    a domain-specific column name unlikely to appear in string literals.
    """
    if tree is None:
        # Cannot verify — conservative block only if the table name appears in raw text.
        for table in snapshot_tables:
            if table.lower() in sql.lower():
                return ContractViolation(
                    rule_id="BR002",
                    rule_name="snapshot_table_temporal_fence",
                    description=(
                        f"SQL could not be parsed and references snapshot table '{table}'; "
                        "cannot verify temporal fence."
                    ),
                    severity="block",
                )
        return None

    referenced_tables = {
        (t.name or "").lower()
        for t in tree.find_all(exp.Table)
    }
    for fqn in snapshot_tables:
        table_name = fqn.split(".")[-1].lower()
        if table_name in referenced_tables:
            if not re.search(r"snapshot_date\s*=", sql, re.IGNORECASE):
                return ContractViolation(
                    rule_id="BR002",
                    rule_name="snapshot_table_temporal_fence",
                    description=(
                        f"Query references snapshot table '{fqn}' without a temporal fence "
                        "(snapshot_date = ...)."
                    ),
                    severity="block",
                )
    return None


def check_non_additive_sum(
    tree: Optional[exp.Expression],
    sql: str,
    non_additive_metrics: list[str],
) -> Optional[ContractViolation]:
    """
    Block SUM(valor) when a non-additive metric filter is present in the SQL.

    The metric-name check is a string search (metric names appear as filter values
    or comments, not as AST-typed nodes). The SUM(valor) check uses the AST to
    find Anonymous or Sum function nodes whose argument is the column 'valor'.
    """
    if tree is None:
        for metric in non_additive_metrics:
            if metric.lower() in sql.lower():
                return ContractViolation(
                    rule_id="BR001",
                    rule_name="non_additive_no_sum",
                    description=(
                        f"SQL could not be parsed and references non-additive metric '{metric}'; "
                        "cannot verify aggregation."
                    ),
                    severity="block",
                )
        return None

    # Detect SUM(valor) in the AST
    sum_of_valor = False
    for func in tree.find_all(exp.Anonymous, exp.Sum):
        if isinstance(func, exp.Sum):
            arg = func.this
            if isinstance(arg, exp.Column) and (arg.name or "").lower() == "valor":
                sum_of_valor = True
                break

    if not sum_of_valor:
        return None

    for metric in non_additive_metrics:
        if metric.lower() in sql.lower():
            return ContractViolation(
                rule_id="BR001",
                rule_name="non_additive_no_sum",
                description=f"SUM(valor) used with non-additive metric '{metric}'.",
                severity="block",
            )
    return None


def check_scope_tag(sql: str, expected_scope: str) -> tuple[bool, bool]:
    """
    Scope tag lives in a SQL comment — regex is correct here (it is not SQL syntax).
    Returns (tag_present, tag_correct).
    """
    match = re.search(r"--\s*scope:\s*(\S+)", sql, re.IGNORECASE)
    if not match:
        return False, False
    return True, match.group(1).lower() == expected_scope.lower()


# ── Main evaluator ─────────────────────────────────────────────────────────────

def run_system_metrics(
    domain: str,
    test_suite_path: str,
    agent_fn,
    snapshot_tables: Optional[list[str]] = None,
    non_additive_metrics: Optional[list[str]] = None,
    output_path: Optional[str] = None,
) -> dict:
    """
    Run Layer 2 evaluation: contract compliance checks on generated SQL.

    Args:
        domain: domain name (e.g. "acme")
        test_suite_path: path to JSON file with test cases
        agent_fn: callable(query, domain) -> (sql, usage)
        snapshot_tables: list of table FQNs that require temporal fences
        non_additive_metrics: list of metric names that must not be SUMmed
        output_path: if set, write results JSON here

    Returns:
        summary dict with contract compliance metrics
    """
    snapshot_tables = snapshot_tables or ["sales.contracts"]
    non_additive_metrics = non_additive_metrics or ["contracts_active", "occupancy"]

    test_cases = json.loads(Path(test_suite_path).read_text())
    results = []

    for tc in test_cases:
        try:
            sql, _ = agent_fn(tc["query"], domain)
            violations = []
            parse_error = False

            if sql:
                tree = _parse(sql)
                if tree is None:
                    parse_error = True

                checkers = [
                    lambda t: check_full_outer_join(t),
                    lambda t: check_union_all(t),
                    lambda t: check_star_select(t),
                    lambda t: check_snapshot_fence(t, sql, snapshot_tables),
                    lambda t: check_non_additive_sum(t, sql, non_additive_metrics),
                ]
                for checker in checkers:
                    v = checker(tree)
                    if v:
                        violations.append(v)

                tag_present, tag_correct = check_scope_tag(sql, domain)
                blocked = any(v.severity == "block" for v in violations)

                result = SystemMetricsResult(
                    test_id=tc["id"],
                    query=tc["query"],
                    generated_sql=sql,
                    violations=violations,
                    scope_tag_present=tag_present,
                    scope_tag_correct=tag_correct,
                    parse_error=parse_error,
                    final_status="blocked" if blocked else ("warned" if violations else "pass"),
                )
            else:
                result = SystemMetricsResult(
                    test_id=tc["id"],
                    query=tc["query"],
                    generated_sql=None,
                    final_status="pass",   # declined queries are compliant
                )

        except Exception as e:
            result = SystemMetricsResult(
                test_id=tc["id"],
                query=tc.get("query", ""),
                generated_sql=None,
                final_status="error",
                error=str(e),
            )

        results.append(result)

    n = len(results)
    n_pass = sum(1 for r in results if r.final_status == "pass")
    n_blocked = sum(1 for r in results if r.final_status == "blocked")
    n_warned = sum(1 for r in results if r.final_status == "warned")
    n_parse_errors = sum(1 for r in results if r.parse_error)
    all_violations = [v for r in results for v in r.violations]
    violation_counts: dict[str, int] = {}
    for v in all_violations:
        violation_counts[v.rule_id] = violation_counts.get(v.rule_id, 0) + 1

    summary = {
        "domain": domain,
        "total_cases": n,
        "pass": n_pass,
        "blocked": n_blocked,
        "warned": n_warned,
        "errors": sum(1 for r in results if r.final_status == "error"),
        "parse_errors": n_parse_errors,
        "contract_compliance_rate": n_pass / n if n else 0,
        "scope_tag_rate": sum(r.scope_tag_present for r in results if r.generated_sql) / n if n else 0,
        "scope_tag_accuracy": sum(r.scope_tag_correct for r in results if r.generated_sql) / n if n else 0,
        "violations_by_rule": violation_counts,
        "results": [asdict(r) for r in results],
    }

    if output_path:
        Path(output_path).write_text(json.dumps(summary, indent=2))
        print(f"Layer 2 results written to {output_path}")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Layer 2: System / contract metrics")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--output", default="evaluation/results_system_metrics.json")
    args = parser.parse_args()

    # Stub: replace with your agent callable.
    # This exists only so the script runs standalone — it does not represent
    # a real agent. See README for the interface contract agent_fn must satisfy.
    def mock_agent_fn(query, domain):
        return ("-- scope: acme\nSELECT region, SUM(valor) FROM sales.orders_v GROUP BY region", {})

    summary = run_system_metrics(args.domain, args.suite, mock_agent_fn, output_path=args.output)
    print(f"Contract compliance:  {summary['contract_compliance_rate']:.1%}")
    print(f"Blocked:             {summary['blocked']}/{summary['total_cases']}")
    print(f"Parse errors:        {summary['parse_errors']}/{summary['total_cases']}")
    print(f"Scope tag accuracy:  {summary['scope_tag_accuracy']:.1%}")
    if summary["violations_by_rule"]:
        print("Violations by rule:")
        for rule, count in summary["violations_by_rule"].items():
            print(f"  {rule}: {count}")
