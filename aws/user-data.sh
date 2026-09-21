#!/bin/bash
# First-boot bootstrap for the agent-trace-lab EC2 instance (AL2023 arm64).
# Installs docker + compose, unpacks the lab, generates the seeded dataset (1,000 traces),
# and registers a systemd unit so the stack auto-starts on every later boot.
set -euxo pipefail

dnf install -y docker git make python3.11 python3.11-pip nc unzip cronie
systemctl enable --now crond

# OpenSearch requires a higher mmap count than the AL2023 default
echo "vm.max_map_count=262144" > /etc/sysctl.d/99-opensearch.conf
sysctl --system

systemctl enable --now docker

# docker compose v2 plugin (not packaged in AL2023)
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-aarch64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Lab code is shipped as a CDK asset (works with a private GitHub repo)
aws s3 cp "__ASSET_S3_URL__" /tmp/lab.zip
mkdir -p /opt/agent-trace-lab
unzip -q /tmp/lab.zip -d /opt/agent-trace-lab
cd /opt/agent-trace-lab
python3.11 -m pip install -r requirements.txt

# Bring the stack up and generate the deterministic dataset (seed 42)
make demo PYTHON=python3.11

# Auto-start the containers on every subsequent boot (stop/start workflow)
cat > /etc/systemd/system/agent-trace-lab.service <<'UNIT'
[Unit]
Description=Agent Trace Lab (OpenSearch + Dashboards + Data Prepper + OTel Collector)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/agent-trace-lab
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose stop

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable agent-trace-lab.service

# Cost failsafe: stop the instance every day at 06:00 UTC in case it is
# left running. Instance shutdown behavior is "stop", so data survives.
# Disable with: sudo rm /etc/cron.d/agent-trace-lab-autostop
echo "0 6 * * * root /usr/sbin/shutdown -h now" > /etc/cron.d/agent-trace-lab-autostop

echo "BOOTSTRAP COMPLETE" > /var/log/agent-trace-lab-ready
