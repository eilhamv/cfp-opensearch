# Instrument your own agent: the minimal span set

The lab's [agent.py](../src/agent_trace_lab/agent.py) is 600 lines because it
simulates a workload. The instrumentation pattern inside it is ~30 lines. This
page is that pattern, extracted — the least you can emit and still run every
investigation in [queries/](../queries/).

## The five spans that matter

| Span | Emitted by | Must carry | Enables |
|---|---|---|---|
| root (one per run) | your entry point | rollups: `agent.tool_retries`, `agent.tool_attempts_total`, plus `gen_ai.usage.total_*` | fleet triage in one query over root spans ([00](../queries/00_triage.ppl), [07](../queries/07_release_comparison.ppl)) |
| `tool.call <name>` | every logical tool/MCP call | `tool.name`, `gen_ai.tool.name` | per-tool slicing |
| `tool.attempt` | **every physical attempt**, as a child of its `tool.call` | `tool.retry.attempt`, `tool.retry.reason`; ERROR status on failed attempts | the retry-storm query ([01](../queries/01_retry_storm.ppl)) — and the audit trail for "did the action apply twice?" |
| `retrieval.search` | each retrieval | `retrieval.document_count` + a coarse `retrieval.document_bucket` | tail latency by workload ([02](../queries/02_latency_percentiles.ppl)) |
| one custom diagnostic span | after generation | your cheap numeric quality signal (here: `context.evidence_overlap_ratio`, `context.overflow_detected`) | evidence-loss detection ([03](../queries/03_evidence_loss.ppl)) |

Two rules do most of the work:

1. **Physical attempts are first-class spans.** Never let a client library
   swallow a retry into one "success". The attempt span is what turns a silent
   retry from an inference into a filterable attribute.
2. **Rollups live on the root.** Fleet questions ("how many runs retried
   last night?") must be answerable from root spans alone — one query, no
   child-span scan.

## The pattern, in code

```python
tracer = provider.get_tracer("my-agent")

def tool_call(name, do_attempt, max_attempts=3):
    with tracer.start_as_current_span(f"tool.call {name}",
            attributes={"tool.name": name, "gen_ai.tool.name": name,
                        "gen_ai.operation.name": "execute_tool"}):
        for n in range(1, max_attempts + 1):
            with tracer.start_as_current_span("tool.attempt",
                    attributes={"tool.name": name,
                                "tool.retry.attempt": n}) as attempt:
                try:
                    return do_attempt()
                except TransientError as e:
                    attempt.set_status(StatusCode.ERROR, str(e))
                    attempt.set_attribute("tool.retry.reason", "timeout")
        raise ToolFailed(name)
```

Then, on the run's root span, before it closes:

```python
root.set_attribute("agent.tool_attempts_total", attempts)
root.set_attribute("agent.tool_retries", retries)
root.set_attribute("gen_ai.usage.total_input_tokens", tokens_in)
root.set_attribute("gen_ai.usage.total_output_tokens", tokens_out)
```

That is the entire trick. Everything else in the lab is workload simulation.

## Multi-agent: one trace, one service per agent

Give each agent its own `TracerProvider` with its own `service.name`
(in-process, spans still nest through shared context — see agent.py's
`setup_tracing()`). Across processes, propagate W3C `traceparent` headers
instead; the span schema does not change. Either way `serviceName` becomes a
query dimension — which is what makes "which agent burns the time?"
([05](../queries/05_agent_breakdown.ppl)) a one-liner.

## Using an agent framework?

If your framework emits OpenTelemetry (natively or via an instrumentation
package), point its OTLP exporter at the Collector in this repo
(`OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector>:4317`) and the pipeline,
indices, and every fleet query work unchanged — spans are spans. What
frameworks usually do **not** give you are the two rules above: per-attempt
child spans under tool calls, and root-span rollups. Add those with a wrapper
like `tool_call()` and a root-span hook; that is the gap this schema fills.

## Before production

- Attribute names: keep the dual strategy — official `gen_ai.*` plus stable
  custom fallbacks ([span-schema.md](span-schema.md)).
- Content never enters spans — counts, buckets, booleans, ratios only.
  Read [privacy-and-redaction.md](privacy-and-redaction.md) first.
