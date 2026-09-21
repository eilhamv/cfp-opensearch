# AWS deployment (CDK)

One `t4g.large` (arm64) EC2 instance running the exact same compose stack,
sized for minimum cost and a fast stop/start cycle.

```
cdk deploy ─→ VPC (public subnet, no NAT) ─→ EC2 t4g.large, 30 GB encrypted gp3
              security group: NO inbound   ─→ user-data: docker compose up + 1,000 traces
              access: SSM port-forward only ─→ systemd unit restarts stack on every boot
```

## Why this shape

- **Managed OpenSearch Service is a non-starter**: domains cannot be stopped,
  so the always-on floor cost defeats the point of a lab you use a few evenings.
- **Fargate** loses the indexed spans on scale-to-zero without adding EFS.
- **A stopped EC2 instance keeps its EBS volume** — the 21,104 indexed spans
  survive stop/start, so resuming takes ~3 minutes with no regeneration.
- **arm64 (t4g)** is ~20% cheaper than x86 and all four images ship arm64 builds.

## Costs (us-east-1, on-demand)

| State | What bills | Rate |
|---|---|---|
| Running | t4g.large + public IPv4 + EBS | ≈ $0.075/hr (~$0.55 per evening of prep) |
| **Stopped** | 30 GB gp3 EBS only | **≈ $2.40/month** |
| Destroyed (`make destroy`) | nothing | $0 |

Failsafes: a cron inside the instance stops it every day at 06:00 UTC in case
it is left running (`sudo rm /etc/cron.d/agent-trace-lab-autostop` to disable),
and every resource carries the tag `project=opensearchcon-agent-trace-lab` so
spend is filterable in Cost Explorer.

## Prerequisites

- AWS CLI v2 configured; [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) installed
- Node.js + `npm i -g aws-cdk`; then once per account/region: `cdk bootstrap`
- No GitHub access needed at boot — the lab code ships to the instance as a
  CDK asset (`user-data.sh` pulls it from the asset bucket), so a private
  clone of this repo deploys fine

## Lifecycle

```bash
make deploy         # first time: ~4 min stack + ~12 min bootstrap (images + traces)
make bootstrap-log  # watch first-boot progress
make tunnel         # Dashboards at http://localhost:5602 (admin / Dem0.Trace.2026)

make stop           # done for the day → ~$2.40/month
make start          # demo-ready again in ~3 min, data intact
make status
make destroy        # remove everything → $0
```

## Security posture

- Security group has **zero inbound rules**; Dashboards is reached only through
  SSM Session Manager port forwarding (IAM-authenticated, TLS, auditable in CloudTrail).
- Instance role carries only `AmazonSSMManagedInstanceCore`.
- EBS encrypted at rest. No keys, no customer data — the workload is synthetic
  (every trace flagged `agent.synthetic=true` on its root span).
