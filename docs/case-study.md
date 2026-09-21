# Case study: the remediation copilot nobody could debug

*Drawn from a production incident-remediation copilot at a major U.S. pay-TV
and streaming provider. This repo carries a deterministic replay of those
failure patterns; the span schema, pipeline, and queries are the production
ones. Company-identifying details are omitted; no production data appears here.*

## The system

When an alert fires, a multi-agent remediation platform is paged before a
human joins the bridge. An **orchestrator** plans and delegates to three
specialists, each its own service: a **diagnosis agent** (log evidence,
ticket/deploy correlation, retrieval), a **remediation agent** that *acts* —
it fetches the approved runbook and executes the rollback — and a **comms
agent** that records the change on the ticket and posts to the bridge. Median
time-to-first-action was the metric it was built to improve — and because this
platform executes changes, every reliability question about it is sharper than
for a chatbot. Each incident produces one distributed trace spanning all four
services.

It worked in staging. Then it met a live-event window: kickoff traffic, a
playback-auth incident, every backing system under load at once — which is
precisely when the copilot is supposed to earn its keep.

## The three complaints

Real words from real reviews, in the order they arrived:

1. **"It's slowest exactly when we need it."** During event windows the copilot
   took roughly twice as long to answer. No errors, no exceptions, nothing in
   the log to point at.
2. **"Dashboards are green but answers are slow."** Mean retrieval latency sat
   inside SLO. Engineers on the bridge were still waiting.
3. **"It answered with a platitude during a real outage."** Mid-incident, the
   copilot replied *"check your service configuration"* — no ticket, no deploy
   ID, no runbook step, despite all of it being retrieved. The complaint was
   filed as **"the agent is sometimes wrong,"** which is not a debuggable
   statement.

## Why the existing telemetry couldn't answer any of them

The team had application logs and a prompt/completion archive. Neither could
resolve a single complaint:

| Question | Why logs and prompt dumps failed |
|---|---|
| Why is it slow under load? | The MCP client retried internally. The log recorded the *outcome* — one success — never the attempts. |
| Where does latency actually break? | A mean over all runs cannot show a boundary that only appears at high document counts. |
| Why was the answer generic? | The prompt archive shows the text sent. It cannot show that the evidence was retrieved and then buried. |

Prompt logs tell you what the model said. They cannot tell you *why the agent
took that path* — because the path is a plan step, a retrieval, four tool
calls with physical attempts underneath them, and a generation. That is a
distributed system, and the team already knew how to debug those.

## What was instrumented

One trace per copilot run, spans for each stage, and three deliberate choices
(full schema in [span-schema.md](span-schema.md)):

- **Physical attempts are first-class.** `tool.call` gets `tool.attempt`
  children carrying `tool.retry.attempt` and `tool.retry.reason`. A retry is no
  longer inferred from duplicate span names — it is an attribute you can filter.
- **Workload shape is recorded, content is not.** `retrieval.search` carries a
  document count and a coarse bucket. Enough to slice latency by load; nothing
  that could leak a customer record.
- **Answer quality gets a cheap numeric signal.** A custom
  `context_usage_check` span records `evidence_overlap_ratio` and
  `overflow_detected` — a triage signal, explicitly *not* a hallucination
  detector, computed in microseconds from data already in hand.

Plus a rollup on the root span (`agent.tool_retries`) so fleet-wide triage is
one query over root spans rather than a scan of every child.

## Complaint → signal → query → fix

This is the whole method. Each row is one investigation, start to finish.

| Complaint | Signal in the span | Query | Fix that shipped |
|---|---|---|---|
| "Slowest when we need it" | `tool.retry.attempt = 2`, first attempt ERROR | [01_retry_storm.ppl](../queries/01_retry_storm.ppl) — **104 of 500 incidents (20.8%)**, all on `rollback_mcp` — the action itself | Raise that client's timeout, add jittered backoff **and idempotency keys** — for a tool that writes, a silent retry is a double-execution risk, and the attempt spans are the audit trail that proves what actually ran. Alert on retry rate. |
| "Green dashboards, slow answers" | `durationInNanos` sliced by `retrieval.document_bucket` | [02_latency_percentiles.ppl](../queries/02_latency_percentiles.ppl) — **p95 ~474ms in the 100+ bucket vs ~70ms in 0–10**, against a ~184ms mean | Cap and paginate retrieval above the boundary and tighten the incident time window. The fix is a workload limit, not "make retrieval faster" — which is what an average would have sent you off to do. |
| "It answered with a platitude" | `overflow_detected = true`, `evidence_overlap_ratio` collapse | [03_evidence_loss.ppl](../queries/03_evidence_loss.ppl) — **0.250 → 0.062, a 4× drop** exactly at the overflow boundary | Rank and deduplicate retrieved documents before assembly; drop routine heartbeat INFO noise; alert when overlap falls below threshold while overflow is true. |
| *(nobody asked)* | token counts per span | [04_cost_by_scenario.ppl](../queries/04_cost_by_scenario.ppl) — the flooded runs are **~10% of traffic, ~26% of spend, 3× per-run cost** | Same fix as above. The context bug was simultaneously the quality bug and the cost bug — invisible until tokens lived in spans. |

There was a fifth finding, only possible because each agent is its own OTel
service: [05_agent_breakdown.ppl](../queries/05_agent_breakdown.ppl) showed
**diagnosis consumed ~71% of time-to-remediation** (~240ms of ~339ms end to
end). Everyone had blamed the LLM; the budget was going to retrieval — which
made the retrieval cap above the highest-leverage fix on the board.

Start with [00_triage.ppl](../queries/00_triage.ppl): one query, one row, three
counts — how much of the fleet is unhealthy and by which signal — then follow
the column into its deep-dive.

## What this cost

Spans carry counts, buckets, booleans, ratios, and token totals — never
prompts, documents, ticket text, or customer identifiers. That is what made
tracing acceptable to a data-protection review in a regulated environment, and
it is why the diagnostic signal is a ratio rather than a copy of the context
(see [privacy-and-redaction.md](privacy-and-redaction.md)).

## The three fixes, as code

In the lab the fixes are the `mode == "after"` branches of
[agent.py](../src/agent_trace_lab/agent.py) — release `2026.09.2` runs the
identical incidents through them:

| Fix | Where | What changes |
|---|---|---|
| Idempotency key on the rollback client | [agent.py:499](../src/agent_trace_lab/agent.py#L499) | the retry becomes safe *and* unnecessary — `rollback_mcp` stops retrying entirely |
| Retrieval capped at the observed boundary | [agent.py:398](../src/agent_trace_lab/agent.py#L398) | no incident pulls a `100+` document window again |
| Dedupe/rank before context assembly | [agent.py:425](../src/agent_trace_lab/agent.py#L425) | heartbeat noise never reaches the context; overflow disappears |

[07_release_comparison.ppl](../queries/07_release_comparison.ppl) is the proof:
104/95/51 → 0/0/0 on the same 500 incidents.

## The transferable part

The failure modes were not exotic AI problems. They were a retry storm, a tail
latency boundary, and a resource being evicted before it was used — three of
the oldest failures in distributed systems, wearing new clothes. What was
missing was the span schema that makes them visible for agents, plus the
willingness to query the fleet instead of staring at one trace.

Reproduce all of it — same numbers, deterministic — with `make demo`.

> **If you present this:** the reproduction numbers here (104/500, 474 ms, 0.250 →
> 0.062) are exact and independently verifiable by anyone who runs the repo.
> Any production before/after figures you quote on stage should be your own
> cleared numbers, not these.
