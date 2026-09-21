# Production sizing: what this looks like at national-provider scale

The question this answers: *"Fine for 500 incidents — what happens at real
traffic?"*

Everything below is a **sizing model with stated inputs and shown arithmetic**,
not a disclosure of any operator's figures. Swap your own numbers into the
inputs and the math still runs. Two of the constants are measured, not
guessed, and are marked as such.

---

## The counterintuitive headline

**Agent traces are small data.** The volume is driven by *incident count*, not
by subscriber or stream count — and incidents are thousands per day, not
millions per second. At the peak modelled below, the whole agent-trace pipeline
runs at **~0.3% of one documented Data Prepper node's throughput** and produces
**tens of megabytes a day**.

You do not need new infrastructure for this. You need an index and a policy.

---

## Inputs (change these to your environment)

| Input | Value used | Basis |
|---|---|---|
| Subscriber base | tens of millions of households | national pay-TV / streaming operator |
| Services in the estate | 300–600 microservices | playback, entitlement, CDN, billing, guide, auth… |
| Raw alert events / day | ~100,000 | pre-dedup, all sources, includes flapping |
| **Deduplicated actionable incidents / day** | **~2,000** | what actually reaches a queue a human or agent works |
| Live-event multiplier | **3–5×** for a ~6-hour window | Sunday NFL-style kickoff windows |
| Spans per incident | **50** | lab emits 21 with 4 agents; production adds tools, deeper retries, more sub-agents |
| **Bytes per span** | **405 B** — *measured* | `_cat/indices` on this lab: 21,104 spans = 8,554,890 B primary |
| Production span inflation | **2×** → ~850 B | richer attributes, longer names; still no prompts or documents |

Peak arithmetic: 2,000/day = **83 incidents/hour** baseline → at 5× =
**~420 incidents/hour ≈ 7 per minute**.

---

## Ingest: the pipeline is oversized by three orders of magnitude

| Metric | Baseline | Event peak (5×) |
|---|---|---|
| Incidents | 83 /hour | 420 /hour |
| Spans | 4,150 /hour = **1.2 /sec** | 21,000 /hour = **5.8 /sec** |
| Even a 10× burst on peak | — | **~58 spans/sec** |

OpenSearch documents a single **r5.xlarge Data Prepper node at 2,100 spans/second
at 20% CPU** ([trace analytics tuning](https://docs.opensearch.org/latest/data-prepper/common-use-cases/trace-analytics/)).

> **Peak load is ~0.3% of one documented node. A 10× burst is ~3%.**

You run **two** Data Prepper nodes — for availability, not throughput. Same for
the Collector gateway.

## Storage: gigabytes, not terabytes

At 810 B/span (measured 405 B × 2 for production richness):

| Volume | Spans/day | Bytes/day | 30-day | 90-day |
|---|---|---|---|---|
| Baseline (2,000 incidents) | 100,000 | **85 MB** | **2.6 GB** | 7.7 GB |
| Sustained 5× | 500,000 | 425 MB | 12.8 GB | 38 GB |
| 10× growth (20,000 incidents/day) | 1,000,000 | 850 MB | 26 GB | 77 GB |

Standard OpenSearch guidance puts shards at 30–50 GB. **Even the 10× case at
90-day retention is a handful of shards** — one rollover index with an ISM
policy, colocated on the observability cluster you already run.

### The comparison that lands

The platform's *own* telemetry — playback, entitlement, CDN and billing logs,
metrics and traces at millions of concurrent streams — is **multi-terabyte per
day**. The agent traces described here are on the order of **0.005% of that**.

> Agent observability is a rounding error on your existing observability bill.
> That is the entire cost argument.

### Sampling

Because the volume is trivial, **keep 100% of agent traces**. This is the
opposite of microservice tracing, where 1–10% head sampling is normal. Every
investigation in this repo depends on complete data: you cannot count "104 of
500 incidents retried" from a 5% sample, and the rare failure is exactly the
one sampling discards.

---

## Compute: the agents, not the pipeline

**Honest caveat:** this lab's runs complete in ~339 ms because the LLM and tool
calls are simulated. **Real runs are seconds, not milliseconds** — LLM calls
dominate. Plan on:

| Sub-agent | Realistic wall clock |
|---|---|
| diagnosis (retrieval + LLM) | 3–8 s |
| remediation (runbook + action) | 2–5 s |
| comms (ticket + bridge post) | 1–2 s |
| **end-to-end per incident** | **~10–20 s** |

Concurrency at peak: 7 incidents/min × 15 s ≈ **2 concurrent incidents**. With
burst headroom and rolling deploys:

| Component | Deployment | Size |
|---|---|---|
| 4 agent services | one deployment each, 2–3 replicas | 1 vCPU / 2 GB per pod |
| OTel Collector | gateway, 2–3 replicas | 1 vCPU / 2 GB |
| Data Prepper | 2 nodes (HA) | r5.xlarge, heap 10 GB, `buffer_size: 4096`, `batch_size: 256`, `workers: 8` (the documented benchmark config) |
| OpenSearch | existing observability cluster | one rollover index + ISM policy |

The agent tier is **~12 small pods**. The bottleneck is the LLM provider's
latency and rate limits — not CPU, not the pipeline.

## Where the money actually goes

At the measured per-run cost profile, LLM spend dominates infrastructure by an
order of magnitude. Which is why the cost query matters: the evidence-loss
pattern costs **3× per run**, so at 2,000 incidents/day it is the difference
between a modest bill and a wasteful one — and it was buying *worse* answers.
Fixing it is the highest-ROI change on the board, and it is only visible
because token counts live in spans.

---

## What changes as you scale up

| Growth | What you change |
|---|---|
| 10× incidents | Nothing structural. Same 2 Data Prepper nodes, more agent replicas. |
| 100× incidents (200k/day) | ~8.5 GB/day, ~580 spans/sec at the 5× peak — still under 30% of one documented Data Prepper node. Add a third node, keep 100% sampling. |
| Multi-region | One Collector gateway per region, cross-region ship to a central cluster, or one cluster per region with cross-cluster search. |
| Retention pressure | ISM: hot 7 days → warm 30 → delete 90. Trace data is investigated within days of an incident, not months. |

---

## The 30-second answer on stage

> "Agent traces are small data — the volume tracks incidents, not subscribers.
> A few thousand incidents a day at 50 spans each is single-digit spans per
> second and tens of megabytes a day. Data Prepper is documented at 2,100 spans
> per second on one r5.xlarge, so we run two nodes for availability, not
> throughput, and the index sits on the observability cluster we already
> operate. And because it's cheap, we keep 100% — you cannot count 104 of 500
> from a 5% sample."
