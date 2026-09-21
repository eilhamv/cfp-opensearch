# Demo runbook

Everything needed to run the demo, in order. Story and rationale live in
[docs/case-study.md](docs/case-study.md); this file is mechanics only.

---

## 1. Bring up the lab

**Laptop** (Docker or Podman, 4 GB free):

```bash
pip install -r requirements.txt
make demo          # stack + 1,000 traces (500 incidents × 2 releases) + validation  (~8 min, plus first-time image pulls)
```

**AWS** (already deployed via `aws/`):

```bash
cd aws
make start  PROFILE=<your-profile>     # ~3 min, data survives stop/start
make tunnel PROFILE=<your-profile>     # leave running in its own terminal
```

Either way the UI is **http://localhost:5602** — user `admin`, password
`Dem0.Trace.2026` (local lab credential; nothing sensitive behind it).

**Which trace UI you get.** This lab runs stock OpenSearch + Dashboards, where
the single-trace view is **Trace Analytics** (verified working here — spans carry
`serviceName`, `traceGroup`, and `durationInNanos`, which is all it needs). The
newer **Agent Traces** view — DAG, Gantt, token rollups — ships in the
[OpenSearch Observability Stack](https://observability.opensearch.org/docs/ai-observability/agent-tracing/)
distribution, not in the stock Dashboards image. It reads the same
`otel-v1-apm-span-*` index this pipeline writes, so if you run that
distribution you get the richer view for free. **Every PPL query in this repo is
identical either way** — the fleet analysis never depended on the UI.

The `Services` tab needs an `otel-v1-apm-service-map` index, which this pipeline
does not build. Use the **Traces** tab.

> The tunnel binds local port 5602. If the laptop stack is also running, stop it
> first (`make down`) or the bind fails.

## 2. Confirm you are demo-ready

Laptop:

```bash
make status                    # expect exactly 21104 spans
make queries                   # runs all ten PPL checks, prints results
make check                     # same ten checks, but ASSERTED — exits non-zero
                               # if any published number is off
```

AWS — the tunnel forwards only 5602, so run the checks on the instance:

```bash
cd aws && make verify PROFILE=<your-profile>
```

If the count is short or the numbers differ: `make reset && make generate`
(laptop), or re-run `make verify` after a minute (AWS, if it was mid-flush).

Grab a retry-affected trace to open on stage (copy the traceId it prints):

```
POST _plugins/_ppl
{"query": "source = otel-v1-apm-span-* | where name = 'incident.remediate' | where `span.attributes.agent@tool_retries` > 0 | fields traceId | head 1"}
```

## 3. Explore

Queries live in [`queries/`](queries/) and each file carries its expected result in a
comment; `make check` asserts all of them against the live cluster. Run them in Query
Workbench (`/app/opensearch-query-workbench`, PPL tab) — paste the PPL raw, results
render as a table.

Start with `00_triage.ppl`, then follow each column into its deep dive.

## 4. If the live demo fails

Numbers to say from memory: **500 incidents · 21,104 spans across 4 services ·
104 rollback retries (20.8%) · 184ms mean vs 474ms p95 · 0.250 → 0.062 ·
diagnosis = 71% of time-to-remediation · 10% of runs = 26% of spend.**
Every result is printed on its slide — narrate the tables. The talk survives
with zero live clicks.

## 5. Shut down

```bash
make down                              # laptop: stop containers, keep data
cd aws && make stop PROFILE=<profile>  # AWS: stops compute; ~$2.40/mo disk only
```

`make clean` (laptop) and `make destroy PROFILE=…` (AWS) remove the data too.
