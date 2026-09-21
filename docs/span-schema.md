# Span Schema

One trace = one agent run. The hierarchy below is emitted for every run;
scenario-specific differences are noted inline.

```
incident.remediate                     service: remediation-orchestrator (root)
│  gen_ai.operation.name = invoke_agent · agent.scenario · agent.run_id
│  rollups: agent.tool_retries · agent.tool_attempts_total ·
│           agent.total_docs_retrieved · agent.context_overlap_ratio ·
│           gen_ai.usage.total_input/output_tokens · total_cost_usd
│
├─ llm.plan                            orchestrator · chat (plan + delegate)
│
├─ agent.run diagnosis                 service: diagnosis-agent · invoke_agent
│  ├─ retrieval.search                   retrieval.document_count · bucket
│  ├─ tool.call opensearch_logs → tool.attempt
│  ├─ tool.call jira_mcp        → tool.attempt   (deploy/ticket correlation)
│  ├─ context.assemble                   final_context_tokens · overflow_detected
│  ├─ llm.generate                       gen_ai.usage.* + llm.* fallbacks
│  └─ context_usage_check                evidence_overlap_ratio  ← CUSTOM span
│
├─ agent.run remediation               service: remediation-agent · invoke_agent
│  │  remediation.action · remediation.change_record
│  ├─ tool.call runbook_mcp     → tool.attempt   (fetch approved action)
│  └─ tool.call rollback_mcp    → tool.attempt(s) (EXECUTES — retries live here;
│                                   attempt 1 carries ERROR + "MCP timeout")
│
└─ agent.run comms                     service: comms-agent · invoke_agent
   ├─ tool.call jira_mcp        → tool.attempt   (write remediation record)
   └─ tool.call slack_mcp       → tool.attempt   (post to the bridge)
```

One incident = one trace = 21 spans (22 when the rollback retries), spread
across four OTel services — which is what makes `serviceName` a query
dimension: per-agent latency and cost need no extra instrumentation.


## Dual-attribute strategy

The OTel GenAI semantic conventions are **experimental** — names can change between
SDK releases. Every LLM span therefore carries both:

| Purpose | Attributes |
|---|---|
| Agent Traces UI classification | `gen_ai.operation.name`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| Stable PPL investigation | `llm.input_tokens`, `llm.output_tokens`, `llm.cost_usd` |

If a convention rename lands, the dashboards may need the new names — but every saved
PPL query keeps working. Both names live in one place in `agent.py`; adapt there.

## Field mapping in OpenSearch

Data Prepper (source `otel_trace_source`, default output format, sink
`index_type: trace-analytics-raw`) writes spans to `otel-v1-apm-span-*`:

- Span attribute `foo.bar.baz` → field `span.attributes.foo@bar@baz`
  (dots inside the attribute key become `@`)
- Top-level span fields stay at the root: `traceId`, `spanId`, `parentSpanId`,
  `name`, `durationInNanos`, `startTime`, `endTime`, `status.code`

So in PPL: `tool.retry.attempt` → `` `span.attributes.tool@retry@attempt` ``
(backticks required because of the `@`).

Durations need no custom attribute — `durationInNanos` is native:
`eval duration_ms = durationInNanos / 1000000`.

## The four scenarios (seed 42, 500 incidents × 2 releases — fully deterministic)

Counts below are per release and live-verified; both releases replay the identical
incident mix. The workload draws from its **own** seeded generator
(`random.Random(42)`), deliberately separate from the global RNG that the OTel
SDK's `RandomIdGenerator` uses for trace/span IDs — so counts are exact on **any**
SDK version, and IDs never collide across releases. (`requirements.txt` still pins
the SDK, but only because the SDK and exporter must share a version to import.)
Realized counts differ from the nominal weights because the draw is random-but-seeded.

| Scenario | Weight | Count (of 500) | Trace signature |
|---|---|---|---|
| `normal` | 55% | 250 | Single attempt per tool |
| `retry_storm` | 17% | 104 (20.8%) | Two `tool.attempt` children under **rollback_mcp**; first has ERROR |
| `slow_retrieval` | 17% | 95 | `retrieval.document_bucket = 100+` on the diagnosis agent |
| `context_underuse` | 11% | 51 | `overflow_detected = true`, overlap ~0.06 |

In release `2026.09.2` the same 500 incidents run with the three fixes applied,
so every signature above disappears: 104/95/51 → 0/0/0
([07_release_comparison.ppl](../queries/07_release_comparison.ppl)).
