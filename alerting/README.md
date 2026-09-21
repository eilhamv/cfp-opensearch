# Alerting monitors

Three OpenSearch [Alerting](https://docs.opensearch.org/latest/observing-your-data/alerting/index/)
monitors that turn the investigations in `queries/` into standing checks.

The point is not that alerting exists — it is **where the alert goes**. These
fire through the same destinations, escalation policy and on-call rotation as
every other production alert in the cluster. Agent failures reach the engineer
who is already awake, instead of a second console nobody watches at 3 a.m.

Create with:

```bash
curl -sk -u admin:<password> -X POST \
  "https://localhost:9201/_plugins/_alerting/monitors" \
  -H 'Content-Type: application/json' -d @01-retry-rate.json
```

Then attach your existing destination/channel to each monitor's trigger action.

| Monitor | Fires when | Investigate with |
|---|---|---|
| `01-retry-rate` | any tool retries in a 15-min window — on an action tool this is a double-execution question | `queries/01_retry_storm.ppl` |
| `02-remediation-ineffective` | the platform keeps erroring after the agent reported success | `queries/06_remediation_effectiveness.ppl` |
| `03-evidence-collapse` | context overflow with evidence overlap below the boundary | `queries/03_evidence_loss.ppl` |

Thresholds are deliberately low for a lab. Set them from your own baseline —
run the matching query over a healthy week first.
