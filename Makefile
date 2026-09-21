# opensearch-agent-trace-lab
# All targets are safe to re-run. See README.md for the full walkthrough.

# Override on any machine: make generate PYTHON=/path/to/python3
PYTHON  ?= python3
AUTH    := admin:Dem0.Trace.2026
OS      := https://localhost:9201

# Use docker compose if available, else podman-compose
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "podman-compose")

.PHONY: up wait generate queries check demo reset status down clean

## Start OpenSearch + Dashboards + Data Prepper + OTel Collector, wait until ready
up:
	$(COMPOSE) up -d
	@$(MAKE) --no-print-directory wait

wait:
	@echo "Waiting for OpenSearch (9201)..."
	@until curl -sk -u '$(AUTH)' '$(OS)/_cluster/health' 2>/dev/null | grep -q '"status"'; do sleep 3; printf "."; done; echo ""
	@curl -sk -u '$(AUTH)' -X PUT '$(OS)/_cluster/settings' -H 'Content-Type: application/json' -d '{"persistent":{"archived.*":null}}' >/dev/null 2>&1 || true
	@echo "Waiting for Data Prepper (21890) + OTel Collector (4317)..."
	@until nc -z localhost 21890 2>/dev/null && nc -z localhost 4317 2>/dev/null; do sleep 2; printf "."; done; echo ""
	@sleep 5
	@echo "Stack ready → http://localhost:5602 (admin / Dem0.Trace.2026)"

## Generate the deterministic dataset — 500 incidents × 2 releases = 1,000 traces (seed=42) — then validate with PPL
generate:
	$(PYTHON) src/agent_trace_lab/agent.py
	@echo "Waiting 20s for Data Prepper batch flush..."
	@sleep 20
	$(PYTHON) queries/find_bugs.py

## Re-run only the PPL validation queries
queries:
	$(PYTHON) queries/find_bugs.py

## Assert every published number against the live cluster — exits non-zero on
## any mismatch. Cross-checks BOTH counts (21,104 spans AND 10,624 log events);
## either alone can hide a failure. Latency is asserted by shape, not millisecond.
check:
	$(PYTHON) queries/find_bugs.py --strict

## One command: deploy + generate + validate
demo: up generate

## Delete indexed spans (stack stays up) — then `make generate` for clean data.
## Data Prepper writes through the otel-v1-apm-span ALIAS (require_alias=true)
## and only creates it at sink startup — so it must be restarted after deletion.
reset:
	@curl -sk -u '$(AUTH)' -X DELETE '$(OS)/otel-v1-apm-span-*' >/dev/null && echo "Deleted otel-v1-apm-span-*"
	@curl -sk -u '$(AUTH)' -X DELETE '$(OS)/otel-v1-apm-service-map*' >/dev/null 2>&1 || true
	@# platform-logs is bulk-loaded by the agent, NOT written through Data Prepper,
	@# so deleting only the span index leaves it behind and the next `make generate`
	@# APPENDS a second copy. The effectiveness query then reports 208 incidents
	@# instead of 104 — every incident_id is a fresh UUID, so nothing looks duplicated.
	@curl -sk -u '$(AUTH)' -X DELETE '$(OS)/platform-logs' >/dev/null 2>&1 && echo "Deleted platform-logs" || true
	@echo "Restarting Data Prepper (recreates the index alias)..."
	@$(COMPOSE) stop data-prepper >/dev/null 2>&1 && $(COMPOSE) start data-prepper >/dev/null 2>&1
	@until nc -z localhost 21890 2>/dev/null; do sleep 2; printf "."; done; echo ""
	@sleep 10
	@echo "Restarting the Collector so its gRPC pipe to Data Prepper is fresh..."
	@# Without this the Collector holds a stale connection and silently drops
	@# the first seconds of spans — a ~2% loss that only shows up as missing
	@# root spans much later.
	@$(COMPOSE) stop otel-collector >/dev/null 2>&1 && $(COMPOSE) start otel-collector >/dev/null 2>&1
	@until nc -z localhost 4317 2>/dev/null; do sleep 2; printf "."; done; echo ""
	@# The port opens well before the Collector's gRPC pipe to Data Prepper is
	@# actually usable. A 10s grace was not enough on a t4g.large: generation
	@# started too early and 2,745 of 21,104 spans were silently dropped (18,359
	@# indexed) with no error anywhere. 45s has been reliable.
	@sleep 45
	@echo "Reset complete — run 'make generate'"

## Containers + indexed span count
status:
	@$(COMPOSE) ps
	@echo "=== Spans indexed ===" && curl -sk -u '$(AUTH)' '$(OS)/otel-v1-apm-span-*/_count' 2>/dev/null && echo ""

## Tear down containers (data volume survives)
down:
	$(COMPOSE) down

## Tear down containers AND delete the data volume (full reset)
clean:
	$(COMPOSE) down -v
