"""
================================================================
OPENSEARCH AGENT TRACE LAB — OpenSearchCon NA 2026
"Your Agent Is a Distributed System: Debugging AI Failures
 with OpenTelemetry and OpenSearch"

A multi-agent incident-remediation platform: an orchestrator delegates to
three specialist agents, each a distinct OTel service, all in ONE trace.

Trace layout per incident (≈21 spans; +1 when the action retries):

  incident.remediate                    remediation-orchestrator   (root)
  ├─ llm.plan                           remediation-orchestrator
  ├─ agent.run diagnosis                diagnosis-agent
  │   ├─ retrieval.search                 doc_count · bucket
  │   ├─ tool.call opensearch_logs → tool.attempt
  │   ├─ tool.call jira_mcp        → tool.attempt      (deploy/ticket correlation)
  │   ├─ context.assemble               tokens · overflow
  │   ├─ llm.generate                   gen_ai.usage.* + llm.*
  │   └─ context_usage_check            evidence_overlap_ratio  ← custom
  ├─ agent.run remediation              remediation-agent
  │   ├─ tool.call runbook_mcp     → tool.attempt      (fetch approved action)
  │   └─ tool.call rollback_mcp    → tool.attempt(s)   (EXECUTE — retries live here)
  └─ agent.run comms                    comms-agent
      ├─ tool.call jira_mcp        → tool.attempt      (write remediation record)
      └─ tool.call slack_mcp      → tool.attempt      (post to the bridge)

Root-span rollups (fleet triage in one query):
  agent.tool_retries · agent.tool_attempts_total · agent.total_docs_retrieved
  agent.context_overlap_ratio · gen_ai.usage.total_* · agent.subagents

Data Prepper field mapping (otel_trace_source default format):
  attribute "foo.bar.baz" → field "span.attributes.foo@bar@baz"

Run:
  make generate          (or: python3 src/agent_trace_lab/agent.py)
================================================================
"""

import json
import time
import random
import hashlib
import uuid
from datetime import datetime, timezone

from opentelemetry import trace, context as otel_context
from opentelemetry.trace import StatusCode
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

# ================================================================
# CONFIG
# ================================================================

OTEL_ENDPOINT = "http://localhost:4317"
OPENSEARCH_HOST = "localhost"
OPENSEARCH_PORT = 9201
OPENSEARCH_AUTH = ("admin", "Dem0.Trace.2026")
PLATFORM_LOG_INDEX = "platform-logs"

SEED = 42

# Workload randomness lives in its OWN generator. The global `random` module is
# deliberately left unseeded: OpenTelemetry's RandomIdGenerator draws from it,
# and seeding it made both release cohorts mint COLLIDING trace IDs (two
# incidents landing in one trace). Keeping them separate means the workload is
# perfectly reproducible AND the determinism no longer depends on which OTel
# SDK version you run.
RNG = random.Random(SEED)
TOTAL_RANDOMIZED = 496   # + 4 explicit scenarios = 500 per release

# Two releases of the remediation platform, 500 incidents each.
#   before — ships the three defects this talk finds
#   after  — same incidents, with the three fixes applied:
#            idempotency key on the rollback client, retrieval capped at the
#            observed boundary, and dedupe/rank before context assembly
RELEASES = [("2026.09.1", "before"), ("2026.09.2", "after")]

SERVICES = ["remediation-orchestrator", "diagnosis-agent",
            "remediation-agent", "comms-agent"]

# ================================================================
# OTEL SETUP — one provider per agent so each is its own service
# ================================================================

_PROVIDERS = {}

def setup_tracing():
    tracers = {}
    for svc in SERVICES:
        provider = TracerProvider(resource=Resource.create({
            "service.name": svc,
            "service.version": "3.0.0",
            "deployment.environment": "lab",
        }))
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)))
        _PROVIDERS[svc] = provider
        tracers[svc] = provider.get_tracer(svc, "3.0.0")
    return tracers


def flush_all():
    for p in _PROVIDERS.values():
        p.force_flush()

# ================================================================
# HELPERS
# ================================================================

def doc_bucket(count: int) -> str:
    if count <= 10:    return "0-10"
    elif count <= 50:  return "11-50"
    elif count <= 100: return "51-100"
    else:              return "100+"


def retrieval_latency(bucket: str) -> float:
    ranges = {
        "0-10":   (0.035, 0.072),
        "11-50":  (0.085, 0.145),
        "51-100": (0.160, 0.270),
        "100+":   (0.320, 0.480),
    }
    lo, hi = ranges[bucket]
    return RNG.uniform(lo, hi)


def keyword_overlap(context: str, answer: str) -> float:
    markers = {"error", "timeout", "circuit", "deploy", "entitlement",
               "breaker", "capacity", "pool", "inc-", "5000"}
    ctx_hits = {w for w in context.lower().split()
                if any(m in w for m in markers)}
    ans_words = set(answer.lower().split())
    return len(ctx_hits & ans_words) / max(len(ctx_hits), 1)

# ================================================================
# TOOL RESPONSES (per owning agent)
# ================================================================

DIAG_TOOLS = {
    "opensearch_logs": {
        "docs": [
            {"level": "ERROR", "message": "Circuit breaker OPEN for entitlement-api",
             "timestamp": "2026-09-22T02:03:00Z", "response_time_ms": 5000},
            {"level": "ERROR", "message": "Timeout calling entitlement service",
             "timestamp": "2026-09-22T02:02:00Z", "response_time_ms": 5000},
            {"level": "WARN",  "message": "Connection pool at 95% capacity",
             "timestamp": "2026-09-22T02:01:00Z", "response_time_ms": 1200},
        ],
    },
    "jira_mcp": {
        "docs": [
            {"ticket": "INC-2391", "status": "In Progress",
             "summary": "Playback auth latency spike",
             "assignee": "oncall-eng", "deployment": "deploy-4522"},
        ],
    },
}

REMED_TOOLS = {
    "runbook_mcp": {
        "docs": [
            {"title": "Runbook: Entitlement API Circuit Breaker", "space": "SRE",
             "excerpt": "If circuit breaker opens: 1) Check health endpoint "
                        "2) Verify connection pool limits 3) Check recent deployments",
             "approved_action": "rollback deploy-4522"},
        ],
    },
    "rollback_mcp": {
        "docs": [
            {"action": "rollback", "target": "deploy-4522",
             "change_record": "CHG-88412", "state": "submitted"},
        ],
    },
}

COMMS_TOOLS = {
    "jira_mcp": {
        "docs": [
            {"ticket": "INC-2391", "update": "remediation recorded",
             "change_record": "CHG-88412"},
        ],
    },
    "slack_mcp": {
        "docs": [
            {"channel": "#incidents", "user": "remediation-copilot",
             "message": "Rolling back deploy-4522 per runbook, change CHG-88412",
             "timestamp": "2026-09-22T02:05:00Z"},
        ],
    },
}

GOOD_ANSWER = (
    "Based on the logs, playback-auth started seeing entitlement service timeouts "
    "at 02:02 UTC, triggered by deploy-4522. The circuit breaker opened after 3 "
    "consecutive failures. Connection pool was at 95% capacity before the outage. "
    "Runbook recommends checking the entitlement-api health endpoint and verifying "
    "connection pool limits."
)

BAD_ANSWER = (
    "Entitlement service issues are commonly caused by network connectivity problems "
    "or misconfigured timeout settings. You should check your service configuration "
    "and ensure proper retry logic is in place."
)

INCIDENTS = [
    "Playback failures spiking on entitlement-api after deploy",
    "Checkout of live-event entitlements timing out region-wide",
    "Circuit breaker open on entitlement-api, subscribers affected",
    "Playback auth latency breaching SLO during event window",
    "Entitlement verification errors climbing on the east region",
    "Login-to-play failures reported by the NOC during kickoff",
    "Deploy-correlated error spike on playback authorization",
]

# ================================================================
# PLATFORM LOGS — the operator's OWN telemetry, same cluster
#
# This is the join that an agent-observability island cannot have: the
# entitlement-api error stream the agent was reading, and the recovery (or
# non-recovery) that followed the action the agent took.
# ================================================================

_LOG_BUFFER = []


def emit_platform_logs(incident_id: str, release: str, deploy_id: str,
                       recovered: bool, t_action: float) -> None:
    """Error stream around one remediation, anchored to the action timestamp."""
    def stamp(offset_s: float) -> str:
        return datetime.fromtimestamp(t_action + offset_s, tz=timezone.utc)\
            .isoformat().replace("+00:00", "Z")

    for i in range(8):                                    # incident in progress
        _LOG_BUFFER.append({
            "@timestamp": stamp(-300 + i * 35),
            "service": "entitlement-api", "level": "ERROR",
            "message": "Circuit breaker OPEN — upstream timeout",
            "deploy_id": deploy_id, "incident_id": incident_id,
            "release": release, "phase": "pre_remediation",
        })

    if recovered:
        for i in range(2):
            _LOG_BUFFER.append({
                "@timestamp": stamp(90 + i * 60),
                "service": "entitlement-api", "level": "INFO",
                "message": "Circuit breaker CLOSED — error rate normal",
                "deploy_id": deploy_id, "incident_id": incident_id,
                "release": release, "phase": "post_remediation",
            })
    else:
        # The rollback was applied twice. The platform never came back —
        # and the agent still reported success.
        for i in range(8):
            _LOG_BUFFER.append({
                "@timestamp": stamp(90 + i * 45),
                "service": "entitlement-api", "level": "ERROR",
                "message": "Circuit breaker OPEN — rollback did not settle",
                "deploy_id": deploy_id, "incident_id": incident_id,
                "release": release, "phase": "post_remediation",
            })


def flush_platform_logs() -> None:
    """Bulk-load the platform's log stream into the same cluster."""
    from opensearchpy import OpenSearch, helpers
    import urllib3
    urllib3.disable_warnings()
    client = OpenSearch(
        hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
        http_auth=OPENSEARCH_AUTH, use_ssl=True,
        verify_certs=False, ssl_show_warn=False)
    if not client.indices.exists(index=PLATFORM_LOG_INDEX):
        client.indices.create(index=PLATFORM_LOG_INDEX, body={"mappings": {"properties": {
            "@timestamp": {"type": "date"},
            "service": {"type": "keyword"}, "level": {"type": "keyword"},
            "message": {"type": "text"}, "deploy_id": {"type": "keyword"},
            "incident_id": {"type": "keyword"}, "release": {"type": "keyword"},
            "phase": {"type": "keyword"},
        }}})
    helpers.bulk(client, ({"_index": PLATFORM_LOG_INDEX, "_source": d}
                          for d in _LOG_BUFFER), chunk_size=2000)
    print(f"{len(_LOG_BUFFER)} platform log events → {PLATFORM_LOG_INDEX}")


# ================================================================
# SPAN EMITTERS
# ================================================================

def _tool_call(tracer, tool_name: str, retry: bool = False):
    """One logical tool call; physical attempts are child spans.
    Returns (attempts, retries)."""
    with tracer.start_as_current_span(
        f"tool.call {tool_name}",
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.provider.name": "synthetic",
            "gen_ai.tool.name": tool_name,
            "tool.name": tool_name,
        },
    ):
        if retry:
            with tracer.start_as_current_span(
                "tool.attempt",
                attributes={"tool.name": tool_name, "gen_ai.tool.name": tool_name,
                            "tool.retry.attempt": 1, "tool.retry.reason": "timeout"},
            ) as a1:
                time.sleep(RNG.uniform(0.08, 0.12))
                a1.set_status(StatusCode.ERROR, "MCP timeout")
            with tracer.start_as_current_span(
                "tool.attempt",
                attributes={"tool.name": tool_name, "gen_ai.tool.name": tool_name,
                            "tool.retry.attempt": 2, "tool.retry.reason": "retry_success"},
            ):
                time.sleep(RNG.uniform(0.02, 0.05))
            return 2, 1
        with tracer.start_as_current_span(
            "tool.attempt",
            attributes={"tool.name": tool_name, "gen_ai.tool.name": tool_name,
                        "tool.retry.attempt": 1, "tool.retry.reason": "success"},
        ):
            time.sleep(RNG.uniform(0.01, 0.03))
        return 1, 0


def run_incident(tracers, scenario: str, incident: str,
                 release: str, mode: str) -> None:
    """
    One incident remediation, end to end, across four agent services.

    scenario:
      "normal"           — clean run
      "retry_storm"      — rollback_mcp (the ACTION) retries silently
      "slow_retrieval"   — diagnosis pulls a 100+ document window
      "context_underuse" — heartbeat noise floods diagnosis context

    mode:
      "before" — the platform as it shipped, with all three defects
      "after"  — the same incidents with the fixes applied: an idempotency key
                 on the rollback client, retrieval capped at the observed
                 boundary, and dedupe before context assembly
    """
    fixed = (mode == "after")
    run_id = str(uuid.uuid4())
    orch = tracers["remediation-orchestrator"]
    diag = tracers["diagnosis-agent"]
    remed = tracers["remediation-agent"]
    comms = tracers["comms-agent"]

    attempts_total = 0
    retries_total = 0

    with orch.start_as_current_span(
        "incident.remediate",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.provider.name": "synthetic",
            "gen_ai.agent.name": "remediation_orchestrator",
            "agent.synthetic": True,
            "agent.scenario": scenario,
            "agent.run_id": run_id,
            "agent.subagents": "diagnosis,remediation,comms",
            "agent.release": release,
            "agent.release_mode": mode,
            "incident.summary": incident,
        },
    ) as root_span:

        # ── orchestrator plans and delegates ─────────────────────
        with orch.start_as_current_span(
            "llm.plan",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "synthetic",
                "llm.plan.delegates_to": "diagnosis,remediation,comms",
            },
        ):
            time.sleep(0.005)

        # ══ DIAGNOSIS AGENT ═══════════════════════════════════════
        with diag.start_as_current_span(
            "agent.run diagnosis",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": "synthetic",
                "gen_ai.agent.name": "diagnosis_agent",
                "agent.scenario": scenario,
            },
        ):
            if scenario == "slow_retrieval":
                doc_count = RNG.randint(110, 180)
                if fixed:                       # FIX: cap at the boundary
                    doc_count = min(doc_count, 100)
            else:
                doc_count = RNG.randint(3, 85)
            bucket = doc_bucket(doc_count)

            with diag.start_as_current_span(
                "retrieval.search",
                attributes={
                    "gen_ai.operation.name": "retrieval",
                    "gen_ai.provider.name": "synthetic",
                    "retrieval.document_count": doc_count,
                    "retrieval.document_bucket": bucket,
                },
            ):
                time.sleep(retrieval_latency(bucket))

            for tool_name in ["opensearch_logs", "jira_mcp"]:
                a, r = _tool_call(diag, tool_name)
                attempts_total += a; retries_total += r

            base_docs = []
            for resp in DIAG_TOOLS.values():
                base_docs.extend(json.dumps(d) for d in resp["docs"])
            base_docs.append(json.dumps(REMED_TOOLS["runbook_mcp"]["docs"][0]))
            context_text = "\n".join(base_docs)

            if scenario == "context_underuse" and not fixed:   # FIX: dedupe/rank
                filler = "\n".join(
                    json.dumps({
                        "level": "INFO", "service": "playback-heartbeat",
                        "message": "Request processed successfully",
                        "response_time_ms": RNG.randint(80, 200),
                        "trace_id": hashlib.md5(str(RNG.random()).encode()).hexdigest(),
                    })
                    for _ in range(60)
                )
                context_text = context_text + "\n" + filler

            ctx_tokens = len(context_text.split())
            overflow = ctx_tokens > 500

            with diag.start_as_current_span(
                "context.assemble",
                attributes={
                    "context.final_context_tokens": ctx_tokens,
                    "context.overflow_detected": overflow,
                },
            ):
                time.sleep(0.005)

            answer = BAD_ANSWER if (scenario == "context_underuse" and overflow) else GOOD_ANSWER
            input_tokens = ctx_tokens + len(incident.split())
            output_tokens = len(answer.split())
            cost_usd = (input_tokens / 1000 * 0.003) + (output_tokens / 1000 * 0.015)

            with diag.start_as_current_span(
                "llm.generate",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "synthetic",
                    "gen_ai.request.model": "anthropic.claude-sonnet-4-20250514-v1:0",
                    "gen_ai.system": "aws.bedrock",
                    "gen_ai.usage.input_tokens": input_tokens,
                    "gen_ai.usage.output_tokens": output_tokens,
                    "llm.input_tokens": input_tokens,
                    "llm.output_tokens": output_tokens,
                    "llm.cost_usd": round(cost_usd, 6),
                    "agent.scenario": scenario,
                    "agent.release": release,
                },
            ):
                time.sleep(0.01)

            overlap_ratio = keyword_overlap(context_text, answer)
            with diag.start_as_current_span(
                "context_usage_check",
                attributes={
                    "context.evidence_overlap_ratio": round(overlap_ratio, 3),
                    "context.overflow_detected": overflow,
                    "context.final_context_tokens": ctx_tokens,
                },
            ):
                pass

        # ══ REMEDIATION AGENT — the one that ACTS ═════════════════
        with remed.start_as_current_span(
            "agent.run remediation",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": "synthetic",
                "gen_ai.agent.name": "remediation_agent",
                "agent.scenario": scenario,
                "remediation.action": "rollback deploy-4522",
                "remediation.change_record": "CHG-88412",
            },
        ):
            a, r = _tool_call(remed, "runbook_mcp")
            attempts_total += a; retries_total += r
            # FIX: an idempotency key makes the retry safe AND unnecessary
            a, r = _tool_call(remed, "rollback_mcp",
                              retry=(scenario == "retry_storm" and not fixed))
            attempts_total += a; retries_total += r

        # ══ COMMS AGENT — record and notify ═══════════════════════
        with comms.start_as_current_span(
            "agent.run comms",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": "synthetic",
                "gen_ai.agent.name": "comms_agent",
                "agent.scenario": scenario,
            },
        ):
            for tool_name in ["jira_mcp", "slack_mcp"]:
                a, r = _tool_call(comms, tool_name)
                attempts_total += a; retries_total += r

        # ── root rollups — fleet triage reads ONLY these ─────────
        root_span.set_attribute("agent.total_tool_calls", 6)
        root_span.set_attribute("agent.tool_attempts_total", attempts_total)
        root_span.set_attribute("agent.tool_retries", retries_total)
        root_span.set_attribute("agent.total_docs_retrieved", doc_count)
        root_span.set_attribute("agent.context_overlap_ratio", round(overlap_ratio, 3))
        root_span.set_attribute("gen_ai.usage.total_input_tokens", input_tokens)
        root_span.set_attribute("gen_ai.usage.total_output_tokens", output_tokens)
        root_span.set_attribute("gen_ai.usage.total_cost_usd", round(cost_usd, 6))
        if overlap_ratio < 0.15:
            root_span.set_attribute("agent.context_ignored", True)

        # ── the platform's own telemetry for this incident ────────
        # A rollback that ran twice never settles: the platform keeps
        # failing while the agent reports success. With the idempotency
        # key in place, every remediation lands.
        recovered = (retries_total == 0)
        root_span.set_attribute("remediation.reported_success", True)
        emit_platform_logs(run_id, release, "deploy-4522", recovered, time.time())


# ================================================================
# RUNNER
# ================================================================

def run_demo() -> None:
    print("Setting up OTel tracing (4 agent services) → http://localhost:4317")
    tracers = setup_tracing()

    explicit = [
        ("normal",           INCIDENTS[0]),
        ("retry_storm",      INCIDENTS[1]),
        ("slow_retrieval",   INCIDENTS[2]),
        ("context_underuse", INCIDENTS[3]),
    ]
    weights = [0.55, 0.17, 0.17, 0.11]
    scenarios = ["normal", "retry_storm", "slow_retrieval", "context_underuse"]

    # The incident population is drawn ONCE, during the first release, and then
    # replayed verbatim for every release after it.
    #
    # Reseeding RNG per release is NOT sufficient on its own, and the difference
    # matters: run_incident() consumes a different NUMBER of draws depending on
    # the scenario and on whether the fixes are active — the context filler alone
    # burns 120 draws, and a retry burns one more than a clean call. So once the
    # "after" cohort stops emitting those, the shared stream desynchronises and
    # every later RNG.choices() picks a different scenario. Measured on the
    # previous implementation: 324 of 500 incidents drew a DIFFERENT scenario in
    # the second release, which quietly made "same 500 incidents, replayed" false.
    #
    # Recording the population fixes that. Release 1's draw order is unchanged
    # (choices -> choice -> run_incident), so its numbers are bit-for-bit what
    # they were; release 2 now genuinely replays the same incidents.
    population: list[tuple[str, str]] = []

    for release, mode in RELEASES:
        global RNG
        RNG = random.Random(SEED)
        print(f"\n── release {release} ({mode}) ──")

        if not population:
            for scenario, incident in explicit:
                population.append((scenario, incident))
                run_incident(tracers, scenario, incident, release, mode)
            for i in range(TOTAL_RANDOMIZED):
                scenario = RNG.choices(scenarios, weights=weights)[0]
                incident = RNG.choice(INCIDENTS)
                population.append((scenario, incident))
                run_incident(tracers, scenario, incident, release, mode)
                if (i + 1) % 100 == 0:
                    print(f"  {i + 1}/{TOTAL_RANDOMIZED}")
        else:
            for i, (scenario, incident) in enumerate(population, 1):
                run_incident(tracers, scenario, incident, release, mode)
                if i % 100 == 0 and i <= TOTAL_RANDOMIZED:
                    print(f"  {i}/{TOTAL_RANDOMIZED}")

    flush_all()
    flush_platform_logs()
    print(f"\n{len(RELEASES) * 500} incident traces across 4 agent services.")
    print("Open http://localhost:5602 → Observability → Trace Analytics")
    print("Then run: make queries")


if __name__ == "__main__":
    run_demo()
