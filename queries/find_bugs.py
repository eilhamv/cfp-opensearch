"""
================================================================
OPENSEARCH AGENT TRACE LAB — PPL investigation queries
Run these live on stage in Query Workbench (raw PPL, results as a table).

Field path convention:
  Python attribute "foo.bar" → OpenSearch field "span.attributes.foo@bar"
  Top-level fields (traceId, name, durationInNanos) stay at root level.

Usage:
  make queries        (or: python3 queries/find_bugs.py)
  Or paste each query from queries/*.ppl into Query Workbench → PPL
  (Dev Tools also works: POST _plugins/_ppl with the JSON body)
================================================================
"""

import json
import urllib3
from opensearchpy import OpenSearch

urllib3.disable_warnings()

client = OpenSearch(
    hosts=[{"host": "localhost", "port": 9201}],
    http_auth=("admin", "Dem0.Trace.2026"),
    use_ssl=True,
    verify_certs=False,
    ssl_show_warn=False,
)

SEP = "=" * 60


def run_ppl(query: str, label: str):
    """Run one PPL query, print it Workbench-style, and return the rows as
    a list of {column: value} dicts (None on error) so --strict can assert."""
    print(f"\n{SEP}")
    print(f"  {label}")
    print(SEP)
    # Print the clean PPL for copy-paste into Query Workbench
    print(f"\nPPL:\n{query}\n")
    try:
        result = client.http.post(
            "/_plugins/_ppl",
            body={"query": query},
        )
        if "datarows" in result:
            cols = [c["name"] for c in result.get("schema", [])]
            col_str = " | ".join(cols)
            print(col_str)
            print("-" * len(col_str))
            for row in result["datarows"]:
                print(" | ".join(str(v) for v in row))
            print(f"\n({len(result['datarows'])} rows)")
            return [dict(zip(cols, row)) for row in result["datarows"]]
        print(json.dumps(result, indent=2)[:800])
    except Exception as exc:
        print(f"Error: {exc}")
        print("→ Make sure the agent has run (make generate) and spans are indexed.")
    return None


def main() -> dict:
    results = {}
    print(SEP)
    print("  AGENT DEBUG QUERIES v2 — OpenSearchCon 2026 Demo")
    print(SEP)
    print("  Field note: Data Prepper stores span attributes as")
    print('  span.attributes.* with "." replaced by "@"')
    print("  e.g. tool.retry.attempt → span.attributes.tool@retry@attempt")

    # ──────────────────────────────────────────────────────────────
    # PREFLIGHT — confirm ingestion before showing anything on stage
    # ──────────────────────────────────────────────────────────────
    results["preflight"] = run_ppl(
        'source = otel-v1-apm-span-*'
        ' | stats count() as total_spans'
        '   by `span.attributes.gen_ai@operation@name`'
        ' | sort - total_spans',
        "PREFLIGHT: span count by operation (verify ingestion worked)",
    )

    # ──────────────────────────────────────────────────────────────
    # TRIAGE — the first query an operator runs (queries/00_triage.ppl)
    #
    # SPEAKER: "Before chasing any one pattern: how much of the fleet is
    # unhealthy, and by which signal? One pass over root spans. Detection
    # uses emitted signals only — never the synthetic scenario label.
    # Scoped to release 2026.09.1, the release under investigation.
    # Expect 500 runs / 104 retry-affected / 95 heavy retrieval / 51 evidence lost."
    # ──────────────────────────────────────────────────────────────
    results["triage"] = run_ppl(
        "source = otel-v1-apm-span-*"
        " | where name = 'incident.remediate'"
        " | where `span.attributes.agent@release` = '2026.09.1'"
        " | eval retried = if(`span.attributes.agent@tool_retries` > 0, 1, 0)"
        " | eval heavy   = if(`span.attributes.agent@total_docs_retrieved` > 100, 1, 0)"
        " | eval lost    = if(`span.attributes.agent@context_overlap_ratio` < 0.15, 1, 0)"
        " | stats count() as runs,"
        "         sum(retried) as retry_affected,"
        "         sum(heavy)   as heavy_retrieval,"
        "         sum(lost)    as evidence_lost",
        "TRIAGE: fleet health by signal — start here",
    )

    # ──────────────────────────────────────────────────────────────
    # SCENARIO 1 — Silent retry storm (Slide 4)
    #
    # SPEAKER: "This query finds traces where a tool had to retry.
    # Each row is one (trace, tool) pair with max_attempt = 2 —
    # meaning the first attempt timed out silently. Expect 104 rows,
    # all rollback_mcp — the ACTION — 20.8% of 500 runs. No error in the
    # application log — the logical tool call succeeded."
    # Grouping is by traceId + tool name only. The per-attempt retry
    # reason stays on each span, visible in the waterfall.
    # ──────────────────────────────────────────────────────────────
    results["retry"] = run_ppl(
        "source = otel-v1-apm-span-*"
        " | where name = 'tool.attempt'"
        " | stats count() as attempts,"
        "         max(`span.attributes.tool@retry@attempt`) as max_attempt"
        "   by traceId, `span.attributes.tool@name`"
        " | where max_attempt > 1"
        " | sort - attempts",
        "SCENARIO 1: Silent retry storm — (trace, tool) with max_attempt > 1",
    )

    # ──────────────────────────────────────────────────────────────
    # SCENARIO 2 — Latency hiding in averages (Slide 5)
    #
    # SPEAKER: "durationInNanos is the native OTel span field —
    # no custom attribute needed. The all-bucket average is ~184ms,
    # which looks fine. Watch the 100+ row: p95 is ~474ms — about
    # 6.7x the 0-10 bucket. That boundary is invisible in the mean."
    # ──────────────────────────────────────────────────────────────
    results["latency"] = run_ppl(
        "source = otel-v1-apm-span-*"
        " | where name = 'retrieval.search'"
        " | eval duration_ms = durationInNanos / 1000000"
        " | stats avg(duration_ms) as avg_ms,"
        "         percentile(duration_ms, 95) as p95_ms,"
        "         percentile(duration_ms, 99) as p99_ms"
        "   by `span.attributes.retrieval@document_bucket`"
        " | sort `span.attributes.retrieval@document_bucket`",
        "SCENARIO 2: Latency by document bucket (p95 vs average)",
    )

    # ──────────────────────────────────────────────────────────────
    # SCENARIO 3 — Context underuse at the boundary (Slide 6)
    #
    # SPEAKER: "The two rows split on context.overflow_detected.
    # When overflow=true, evidence_overlap drops from 0.250 to 0.062
    # — a 4x collapse at the overflow boundary (949 vs 51 checks).
    # That's not 'the agent is wrong' — that's a measurable boundary."
    # ──────────────────────────────────────────────────────────────
    results["evidence"] = run_ppl(
        "source = otel-v1-apm-span-*"
        " | where name = 'context_usage_check'"
        " | stats avg(`span.attributes.context@evidence_overlap_ratio`) as avg_overlap,"
        "         avg(`span.attributes.context@final_context_tokens`) as avg_tokens,"
        "         count() as checks"
        "   by `span.attributes.context@overflow_detected`",
        "SCENARIO 3: Context underuse — overlap ratio vs overflow flag",
    )

    # ──────────────────────────────────────────────────────────────
    # BONUS — Cost by failure scenario (Slide 7)
    #
    # SPEAKER: "Same data, 6 lines of PPL. context_underuse is 10%
    # of runs but 26% of total spend — each run costs 3x a normal
    # run, and returns a WORSE answer. Token bloat is a cost bug and
    # a quality bug at the same time. Invisible until token counts
    # live in spans. Scoped to 2026.09.1.
    # Expect: context_underuse 51 runs at $0.0028/run, the rest ~$0.0009."
    # ──────────────────────────────────────────────────────────────
    results["cost"] = run_ppl(
        "source = otel-v1-apm-span-*"
        " | where name = 'llm.generate'"
        " | where `span.attributes.agent@release` = '2026.09.1'"
        " | eval failure_pattern = `span.attributes.agent@scenario`"
        " | stats sum(`span.attributes.llm@input_tokens`)  as total_in,"
        "         sum(`span.attributes.llm@output_tokens`) as total_out,"
        "         count() as runs"
        "   by failure_pattern"
        " | eval est_cost_usd ="
        "     (total_in  * 0.000003)"
        "   + (total_out * 0.000015)"
        " | eval cost_per_run = est_cost_usd / runs"
        " | sort - cost_per_run",
        "COST: attribution by failure pattern (total + per-run)",
    )

    # ──────────────────────────────────────────────────────────────
    # AGENT BREAKDOWN — which agent is the bottleneck (multi-agent only)
    # ──────────────────────────────────────────────────────────────
    results["breakdown"] = run_ppl(
        "source = otel-v1-apm-span-*"
        " | where name like 'agent.run%' or name = 'incident.remediate'"
        " | eval duration_ms = durationInNanos / 1000000"
        " | stats count() as runs,"
        "         avg(duration_ms) as avg_ms,"
        "         percentile(duration_ms, 95) as p95_ms"
        "   by serviceName"
        " | sort - avg_ms",
        "AGENT BREAKDOWN: latency by sub-agent service",
    )

    # ──────────────────────────────────────────────────────────────
    # EFFECTIVENESS — did the platform actually recover? (Slide 9)
    #
    # SPEAKER: "The agent reported success on all 500. This reads the
    # platform's OWN entitlement-api error stream — same cluster — and
    # finds the incidents still erroring AFTER the rollback.
    # Expect 104 incidents, 8 errors each — exactly the 104 that
    # double-executed. The query no agent-observability island can run."
    # ──────────────────────────────────────────────────────────────
    results["effectiveness"] = run_ppl(
        "source = platform-logs"
        " | where level = 'ERROR' and phase = 'post_remediation'"
        " | where release = '2026.09.1'"
        " | stats count() as errors_after_remediation by incident_id"
        " | sort - errors_after_remediation",
        "EFFECTIVENESS: incidents still erroring after the agent reported success",
    )

    # ──────────────────────────────────────────────────────────────
    # THE JOIN — identity match, not count correlation (follows Slide 9)
    #
    # SPEAKER: "104 equals 104 could be coincidence. This is the receipt:
    # the agent's root spans JOINED to the platform's error stream on
    # run_id = incident_id. Same 104 incidents, by identity — one query,
    # because both sides live in one cluster."
    # ──────────────────────────────────────────────────────────────
    results["join"] = run_ppl(
        "source = otel-v1-apm-span-*"
        " | where name = 'incident.remediate' and `span.attributes.agent@release` = '2026.09.1'"
        " | where `span.attributes.agent@tool_retries` > 0"
        " | join ON `span.attributes.agent@run_id` = incident_id"
        "   [ source = platform-logs"
        "     | where level = 'ERROR' and phase = 'post_remediation'"
        "     | stats count() as errors_after by incident_id ]"
        " | fields `span.attributes.agent@run_id`, errors_after"
        " | sort `span.attributes.agent@run_id`",
        "THE JOIN: retried incidents matched by IDENTITY to unrecovered errors",
    )

    # ──────────────────────────────────────────────────────────────
    # RELEASE COMPARISON — same 500 incidents, fixes applied (Slide 10)
    #
    # SPEAKER: "Idempotency key, retrieval cap, dedupe before assembly.
    # Expect 2026.09.1 -> 104/95/51 and 2026.09.2 -> 0/0/0."
    # ──────────────────────────────────────────────────────────────
    results["release"] = run_ppl(
        "source = otel-v1-apm-span-*"
        " | where name = 'incident.remediate'"
        " | eval release = `span.attributes.agent@release`"
        " | eval retried = if(`span.attributes.agent@tool_retries` > 0, 1, 0)"
        " | eval heavy   = if(`span.attributes.agent@total_docs_retrieved` > 100, 1, 0)"
        " | eval lost    = if(`span.attributes.agent@context_overlap_ratio` < 0.15, 1, 0)"
        " | stats count() as incidents,"
        "         sum(retried) as retry_affected,"
        "         sum(heavy)   as heavy_retrieval,"
        "         sum(lost)    as evidence_lost"
        "   by release"
        " | sort release",
        "RELEASE COMPARISON: before/after — the fixes, proven",
    )

    print(f"\n{SEP}")
    print("  Done. Paste any query above into Query Workbench → PPL")
    print(SEP)
    print("\nTrace Analytics filters to try:")
    print("  tool.name:jira_mcp                     → all Jira tool calls")
    print("  retrieval.document_bucket:100+         → slow retrieval traces")
    print("  context.overflow_detected:true         → context overflow traces")
    print("  agent.scenario:retry_storm             → retry storm traces")
    return results



# ════════════════════════════════════════════════════════════════
# --strict: assert every expected result, exit non-zero on mismatch.
#
# This encodes the lab's own discipline: one green query proves little,
# so the suite cross-checks BOTH independent sources (span count AND
# platform-log count) and every published number. Counts, ratios, tokens
# and costs are exact for seed 42; latency is wall-clock, so only its
# SHAPE is asserted (rising buckets, ~6.5x p95 gap) — never a millisecond.
# ════════════════════════════════════════════════════════════════

EXPECTED_SPANS = 21_104
EXPECTED_LOGS = 10_624


def _num(v):
    """PPL may hand numerics back as int, float, or string."""
    return float(v)


def _bool(v):
    """PPL returns booleans as true bools or as 'true'/'false' strings."""
    return v if isinstance(v, bool) else str(v).strip().lower() == "true"


def settle() -> None:
    """Wait for ingestion to finish before querying. Data Prepper assembles
    whole traces and flushes on its own schedule, and the Collector now
    retries forever under backpressure — so on a slow machine the data is
    LATE, not lost. Running the queries early photographs a half-filled
    index and every derived number comes out short. Poll until the span
    count reaches the expected total or stops moving for a full minute."""
    import time
    prev, stable = -1, 0
    for i in range(120):                    # up to ~10 minutes
        try:
            cur = client.count(index="otel-v1-apm-span-*")["count"]
        except Exception:
            cur = -1
        if cur == EXPECTED_SPANS:
            print(f"  settled: span count {cur}")
            return
        stable = stable + 1 if cur == prev else 0
        prev = cur
        if stable >= 12:                    # unchanged for a full minute —
            break                           # long enough to outlast retry backoff
        if i % 6 == 0:
            print(f"  settling… span count {cur}")
        time.sleep(5)
    print(f"  settle gave up at span count {prev} (expected {EXPECTED_SPANS})")


def validate(results: dict) -> int:
    failures = []

    def check(cond: bool, ok: str, bad: str) -> None:
        if cond:
            print(f"  PASS  {ok}")
        else:
            failures.append(bad)
            print(f"  FAIL  {bad}")

    print(f"\n{SEP}\n  STRICT VALIDATION\n{SEP}")

    # Cross-check the two independent sources first — either alone hides a failure.
    spans = client.count(index="otel-v1-apm-span-*")["count"]
    logs = client.count(index="platform-logs")["count"]
    check(spans == EXPECTED_SPANS, f"spans indexed = {spans}",
          f"spans indexed = {spans}, expected {EXPECTED_SPANS}")
    check(logs == EXPECTED_LOGS, f"platform-log events = {logs}",
          f"platform-log events = {logs}, expected {EXPECTED_LOGS}")

    if any(results.get(k) is None for k in results):
        missing = [k for k, v in results.items() if v is None]
        failures.append(f"queries returned no result: {missing}")
        print(f"  FAIL  queries returned no result: {missing}")

    # Preflight histogram — sums to 21,104
    if results.get("preflight"):
        hist = {r["span.attributes.gen_ai@operation@name"]: r["total_spans"]
                for r in results["preflight"]}
        want = {"execute_tool": 6000, "invoke_agent": 4000,
                "chat": 2000, "retrieval": 1000, None: 8104}
        check(hist == want, "preflight histogram 6000/4000/2000/1000/null 8104",
              f"preflight histogram {hist} != {want}")

    # Triage — 500 / 104 / 95 / 51
    if results.get("triage"):
        t = results["triage"][0]
        got = (t["runs"], t["retry_affected"], t["heavy_retrieval"], t["evidence_lost"])
        check(got == (500, 104, 95, 51), "triage 500/104/95/51",
              f"triage {got} != (500, 104, 95, 51)")

    # Retry storm — 104 rows, all rollback_mcp, attempts=2
    if results.get("retry"):
        rows = results["retry"]
        tools = {r["span.attributes.tool@name"] for r in rows}
        ok = (len(rows) == 104 and tools == {"rollback_mcp"}
              and all(_num(r["attempts"]) == 2 and _num(r["max_attempt"]) == 2
                      for r in rows))
        check(ok, "retry storm: 104 rows, all rollback_mcp, attempts=2",
              f"retry storm: {len(rows)} rows, tools={tools}")

    # Latency — shape only (wall-clock): rising buckets, p95 gap in [4, 10]
    if results.get("latency"):
        by_bucket = {r["span.attributes.retrieval@document_bucket"]: r
                     for r in results["latency"]}
        order = ["0-10", "11-50", "51-100", "100+"]
        ok = list(by_bucket) is not None and set(by_bucket) == set(order)
        if ok:
            p95 = [by_bucket[b]["p95_ms"] for b in order]
            avg = [by_bucket[b]["avg_ms"] for b in order]
            gap = p95[3] / p95[0]
            ok = (p95 == sorted(p95) and avg == sorted(avg) and 4 <= gap <= 10)
            check(ok, f"latency shape: rising buckets, p95 gap {gap:.1f}x",
                  f"latency shape broken: p95={p95}, gap={p95[3]/p95[0]:.1f}x")
        else:
            check(False, "", f"latency buckets {set(by_bucket)} != {set(order)}")

    # Evidence loss — 949 @ 0.250 / 78 tok vs 51 @ 0.062 / 798 tok
    if results.get("evidence"):
        by_flag = {_bool(r["span.attributes.context@overflow_detected"]): r
                   for r in results["evidence"]}
        try:
            f, t = by_flag[False], by_flag[True]
            ok = (f["checks"] == 949 and t["checks"] == 51
                  and round(f["avg_overlap"], 3) == 0.250
                  and round(t["avg_overlap"], 3) == 0.062
                  and round(f["avg_tokens"]) == 78 and round(t["avg_tokens"]) == 798)
            check(ok, "evidence: 949 @ 0.250/78tok vs 51 @ 0.062/798tok",
                  f"evidence: false={f}, true={t}")
        except KeyError:
            check(False, "", f"evidence rows missing a flag: {list(by_flag)}")

    # Cost — runs per pattern exact; context_underuse ≥ 2.5x every other pattern
    if results.get("cost"):
        by_pat = {r["failure_pattern"]: r for r in results["cost"]}
        runs = {k: v["runs"] for k, v in by_pat.items()}
        want_runs = {"normal": 250, "retry_storm": 104,
                     "slow_retrieval": 95, "context_underuse": 51}
        ratio_ok = ("context_underuse" in by_pat and all(
            by_pat["context_underuse"]["cost_per_run"] >= 2.5 * v["cost_per_run"]
            for k, v in by_pat.items() if k != "context_underuse"))
        check(runs == want_runs and ratio_ok,
              "cost: runs 250/104/95/51, context_underuse ≥ 2.5x per run",
              f"cost: runs={runs}, ratio_ok={ratio_ok}")

    # Agent breakdown — 1,000 runs each; ordering; diagnosis 60–80% of end-to-end
    if results.get("breakdown"):
        by_svc = {r["serviceName"]: r for r in results["breakdown"]}
        want_svc = {"remediation-orchestrator", "diagnosis-agent",
                    "remediation-agent", "comms-agent"}
        ok = set(by_svc) == want_svc and all(v["runs"] == 1000 for v in by_svc.values())
        if ok:
            o, d = by_svc["remediation-orchestrator"]["avg_ms"], by_svc["diagnosis-agent"]["avg_ms"]
            r, c = by_svc["remediation-agent"]["avg_ms"], by_svc["comms-agent"]["avg_ms"]
            share = d / o
            ok = o > d > r > c and 0.60 <= share <= 0.80
            check(ok, f"breakdown: orch > diag > remed > comms, diagnosis {share:.0%}",
                  f"breakdown: avgs o={o:.0f} d={d:.0f} r={r:.0f} c={c:.0f}, share={share:.0%}")
        else:
            check(False, "", f"breakdown services/runs wrong: "
                  f"{ {k: v['runs'] for k, v in by_svc.items()} }")

    # Effectiveness — 104 incidents, 8 post-remediation errors each
    if results.get("effectiveness"):
        rows = results["effectiveness"]
        ok = len(rows) == 104 and all(r["errors_after_remediation"] == 8 for r in rows)
        check(ok, "effectiveness: 104 incidents never recovered, 8 errors each",
              f"effectiveness: {len(rows)} rows")

    # The join — the same 104, matched by identity across both indices
    if results.get("join"):
        rows = results["join"]
        ok = len(rows) == 104 and all(_num(r["errors_after"]) == 8 for r in rows)
        check(ok, "join: 104 retried incidents = 104 unrecovered, by identity",
              f"join: {len(rows)} rows")

    # Release comparison — 104/95/51 → 0/0/0 on the same 500
    if results.get("release"):
        by_rel = {r["release"]: r for r in results["release"]}
        def tup(rel):
            r = by_rel.get(rel, {})
            return (r.get("incidents"), r.get("retry_affected"),
                    r.get("heavy_retrieval"), r.get("evidence_lost"))
        ok = tup("2026.09.1") == (500, 104, 95, 51) and tup("2026.09.2") == (500, 0, 0, 0)
        check(ok, "release: 2026.09.1 → 104/95/51 · 2026.09.2 → 0/0/0",
              f"release: 09.1={tup('2026.09.1')}, 09.2={tup('2026.09.2')}")

    print(f"\n{SEP}")
    if failures:
        print(f"  STRICT VALIDATION FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"    ✗ {f}")
        print(SEP)
        return 1
    print("  STRICT VALIDATION PASSED — every published number verified live.")
    print(SEP)
    return 0


if __name__ == "__main__":
    import sys
    if "--strict" in sys.argv:
        print(f"{SEP}\n  SETTLE — wait for ingestion before querying\n{SEP}")
        settle()
        collected = main()
        sys.exit(validate(collected))
    main()
