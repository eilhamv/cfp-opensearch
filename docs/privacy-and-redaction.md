# Privacy and Redaction

This lab is designed so that **no sensitive content ever enters a span** — and so that
the same discipline carries over when you swap the simulated tools for real MCP servers.

## What the lab's spans contain

| In spans | Never in spans |
|---|---|
| Scenario labels (`agent.scenario`) | Raw prompts or completions |
| Token counts (`gen_ai.usage.*`, `llm.*`) | Retrieved document bodies |
| Cost estimates (`llm.cost_usd`) | Ticket text, Slack messages, wiki content |
| Document-count buckets (`retrieval.document_bucket`) | API responses or payloads |
| Tool names (`tool.name`) | Secrets, credentials, tokens |
| Boolean diagnostics (`context.overflow_detected`) | Customer or user identifiers |
| Ratios (`context.evidence_overlap_ratio`) | Internal hostnames beyond the local lab |

Every trace is also flagged `agent.synthetic = true` on its root span, so simulated
data can never be mistaken for production telemetry in a shared cluster (fleet
queries read root spans, which is where the flag lives).

> Note: the root span carries `incident.summary`. In this lab it is one of seven fixed
> synthetic strings. **In production, drop, hash, or template this attribute** — a real
> incident summary is content, not metadata, and may quote alert text or hostnames.

## Design rules that make this work

1. **Measure content, don't copy it.** Instead of storing retrieved documents, store
   `retrieval.document_count` and a coarse `retrieval.document_bucket`. Instead of the
   assembled context, store `context.final_context_tokens` and a computed
   `context.evidence_overlap_ratio`.

2. **Diagnostics as booleans and ratios.** `context.overflow_detected=true` is exactly
   as queryable as the overflowing context itself — with none of the exposure.

3. **Buckets beat raw values** when the raw value could fingerprint a customer
   (document counts, latencies near SLA boundaries, etc.).

## Enforcing redaction in the pipeline (defense in depth)

Instrumentation discipline is the first line; the pipeline is the second:

- **OTel Collector** — use the `attributes` processor to delete or hash keys before
  they leave the host:

  ```yaml
  processors:
    attributes/redact:
      actions:
        - key: incident.summary
          action: delete
        - key: gen_ai.prompt
          action: delete
  ```

- **Data Prepper** — the pipeline can drop or mutate attributes before the OpenSearch
  sink (e.g., `delete_entries` processor), so even a misbehaving SDK upstream cannot
  index raw content.

- **OpenSearch** — index-level field masking / FLS via the Security plugin as the final
  backstop for operator access.

## When you adapt this lab to real tools

Before pointing the instrumentation at real MCP servers, decide per attribute:
*metadata* (keep), *content* (drop), or *identifier* (hash or bucket). If an attribute
could appear in a GDPR/CCPA subject-access request, it does not belong in a span.
