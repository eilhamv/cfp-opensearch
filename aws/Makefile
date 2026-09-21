# AWS lifecycle for the agent-trace-lab.
# Prereqs: AWS CLI configured, `cdk bootstrap` run once in the target
# account/region, Session Manager plugin installed for `make tunnel`.

STACK := AgentTraceLab
# Pass PROFILE=<name> to use a named AWS CLI profile (e.g. make start PROFILE=projcast)
PROFILE ?=
AWSOPTS := $(if $(PROFILE),--profile $(PROFILE),)
ID = $(shell aws cloudformation describe-stacks --stack-name $(STACK) $(AWSOPTS) \
       --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)

.PHONY: deploy start stop status tunnel verify bootstrap-log destroy

## Create everything (first boot generates the dataset — allow ~12 min)
deploy:
	pip install -r requirements.txt
	cdk deploy --require-approval never $(if $(PROFILE),--profile $(PROFILE),)

## Resume a stopped instance (data intact; stack up in ~3 min)
start:
	aws ec2 $(AWSOPTS) start-instances --instance-ids $(ID)
	@echo "Wait ~3 min, then: make tunnel  →  http://localhost:5602"

## Stop the instance — from here on you pay only for the 30 GB disk
stop:
	aws ec2 $(AWSOPTS) stop-instances --instance-ids $(ID)

status:
	@aws ec2 $(AWSOPTS) describe-instances --instance-ids $(ID) \
	  --query "Reservations[0].Instances[0].State.Name" --output text

## Port-forward Dashboards to http://localhost:5602 (no open ports needed)
tunnel:
	aws ssm $(AWSOPTS) start-session --target $(ID) \
	  --document-name AWS-StartPortForwardingSession \
	  --parameters '{"portNumber":["5602"],"localPortNumber":["5602"]}'

## Run the ten PPL validations ON the instance (the tunnel only forwards 5602,
## so the local `make queries` cannot reach the cluster's 9201 — use this instead)
verify:
	@# Poll rather than sleeping a fixed interval: the suite is ten queries now
	@# (retry and effectiveness return 104 rows each) and a fixed 25s wait came
	@# back while the command was still InProgress, printing truncated output
	@# that reads exactly like a broken demo.
	@CID=$$(aws ssm $(AWSOPTS) send-command --instance-ids $(ID) \
	   --document-name AWS-RunShellScript \
	   --parameters 'commands=["cd /opt/agent-trace-lab && python3.11 queries/find_bugs.py"]' \
	   --query 'Command.CommandId' --output text); \
	 printf "running ten PPL validations on the instance"; \
	 while [ "$$(aws ssm $(AWSOPTS) get-command-invocation --command-id $$CID \
	       --instance-id $(ID) --query Status --output text 2>/dev/null)" = "InProgress" ]; do \
	   printf "."; sleep 5; done; echo ""; \
	 aws ssm $(AWSOPTS) get-command-invocation --command-id $$CID \
	   --instance-id $(ID) --query StandardOutputContent --output text

## Watch the first-boot bootstrap (image pulls + trace generation)
bootstrap-log:
	aws ssm $(AWSOPTS) start-session --target $(ID) \
	  --document-name AWS-StartInteractiveCommand \
	  --parameters '{"command":["tail -f /var/log/cloud-init-output.log"]}'

## Delete everything — cost drops to $0
destroy:
	cdk destroy --force $(if $(PROFILE),--profile $(PROFILE),)
