# "Couldn't Langfuse do this?"

Mostly yes — and pretending otherwise would be dishonest. This document states
plainly what is swappable, what is not, and when you should pick the other tool.

## What a dedicated LLM-observability platform already does well

[Langfuse](https://langfuse.com/docs) (open source, self-hostable, acquired by
ClickHouse in January 2026), LangSmith, Arize Phoenix and others give you, out
of the box and better than a hand-rolled setup:

- **OTel-native ingest.** Langfuse accepts OTLP directly, so the instrumentation
  in this repo would flow into it essentially unchanged.
- **Multi-agent trace trees and graph views**, with automatic rendering of
  framework structures like LangGraph — nicer than what stock Trace Analytics
  gives you today.
- **Prompt/completion capture, evaluations, prompt management, playgrounds,
  annotation queues, session views.** None of that is in this repo, and building
  it would be foolish.
- **Token and cost dashboards** without writing a query.

If your question is *"how is my agent's reasoning behaving?"* — prompt quality,
eval scores, regressions between prompt versions — **use one of those tools.**
This talk will not help you.

## What does not port

One thing, and it is architectural rather than a feature gap:

> **The agent's telemetry lives in the same cluster as the telemetry of the
> systems the agent operates on.**

Our agent reads `entitlement-api` error logs, correlates `deploy-4522`, and
executes a rollback. Those logs, that deploy marker, that incident timeline are
**already in OpenSearch** at any operator running this stack. So this query is
possible:

```sql
source = platform-logs
| where level = 'ERROR' and phase = 'post_remediation'
| stats count() as errors_after_remediation by incident_id
| sort - errors_after_remediation
```

It answers a question about the *agent* using the *platform's* data: **the agent
reported success on every incident — did the platform actually recover?** Here,
104 incidents kept failing after the agent's rollback, and every one of them is a
rollback that silently ran twice. And "every one of them" is not a count
coincidence: [08_effectiveness_join.ppl](../queries/08_effectiveness_join.ppl)
is a literal PPL `join` of the retried root spans onto that error stream on
`run_id = incident_id` — the same 104, matched by identity, in a single query.
A tool that holds only the agent's side has no right-hand table to join.

An LLM-observability platform holds agent traces. It does not hold your
entitlement-api error stream, your deploy events, or your incident timeline —
so it can tell you *what the agent did* and never *whether it worked*. Shipping
your production logs into it to close that gap means paying to duplicate the
telemetry estate you already run.

For a **read-only assistant**, this does not matter much. For an agent that
**takes actions on production systems**, verifying the outcome is the entire
point — and that verification requires the join.

## Secondary, still real

- **One alerting pipeline.** Monitors on agent signals fire through the same
  routing as every other production alert. Agent failures page the on-call
  rotation that is already awake, instead of a second console nobody watches
  at 3 a.m.
- **One retention, access-control and residency story.** No second data
  processor, no prompts leaving the estate, one security review. That is what
  made tracing acceptable in a regulated environment here — see
  [privacy-and-redaction.md](privacy-and-redaction.md).
- **Deterministic reproduction.** This repo replays a fixed incident population
  so a fix can be proven, not asserted — see
  [07_release_comparison.ppl](../queries/07_release_comparison.ppl). No hosted
  product gives you a reproducible incident lab.

## Honest recommendation

| Situation | Use |
|---|---|
| Prompt iteration, evals, regression scoring, annotation | A dedicated LLM-obs platform |
| Read-only assistant, no production side effects | Either — pick the nicer UI |
| **Agent takes actions on systems you already observe in OpenSearch** | **This approach** |
| Both needs | Both. It is the same OTel data; fan out the exporter. |

That last row is the real answer for most teams, and nothing here argues
otherwise. The claim of this talk is narrow and defensible: **when your agent
acts on infrastructure whose telemetry you already own, keeping the agent's
traces in that same cluster buys you a class of question no separate tool can
answer.**
