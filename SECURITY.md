# Security

## The credential in this repository is deliberate

`Dem0.Trace.2026` appears in `docker-compose.yml`, the `Makefile`, `DEMO.md` and a few
other files. It is the admin password for a **single-node OpenSearch container that runs
on your own machine**, created fresh by `make up` and destroyed by `make clean`. It is
published on purpose so the lab starts with one command.

It is not a secret, it protects nothing, and it is not reused anywhere. Treat any scanner
alert on it as a false positive.

**Do not point this configuration at anything real.** The container runs with
`discovery.type=single-node`, TLS verification disabled between components, and no
network policy. It is a laboratory, not a deployment template.

## Before you run this against production data

Read [docs/privacy-and-redaction.md](docs/privacy-and-redaction.md) first. The span schema
here is deliberately content-free — counts, buckets, booleans, ratios and token totals,
never prompts, retrieved documents, ticket text or customer identifiers — and that
property is what makes agent tracing survive a data-protection review. If you adapt the
instrumentation, decide per attribute whether it is *metadata* (keep), *content* (drop) or
an *identifier* (hash or bucket).

The optional AWS deployment in [`aws/`](aws/) is built to the same standard: a security
group with **zero inbound rules**, access only through SSM Session Manager port
forwarding, an encrypted EBS volume, and an instance role carrying nothing but
`AmazonSSMManagedInstanceCore`.

## Reporting a vulnerability

If you find a genuine security problem in this repository — not the lab credential above —
please report it privately rather than opening a public issue:

- Use GitHub's [private vulnerability reporting](https://github.com/eilhamv/cfp-opensearch/security/advisories/new), or
- Send a direct message to [@eilhamv](https://github.com/eilhamv) on GitHub.

Expect an acknowledgement within a week. This is a conference companion repository
maintained by one person, not a supported product — please set your expectations for
response time accordingly.

## Scope

In scope: anything in this repository — the agent, the pipeline configuration, the
queries, the alerting monitors, the CDK stack.

Out of scope: vulnerabilities in OpenSearch, Data Prepper, the OpenTelemetry Collector or
their dependencies. Report those to the respective projects.
