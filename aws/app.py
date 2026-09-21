"""CDK app: single-instance AWS deployment of the agent-trace-lab.

Design goals (in order): minimum cost, fast stop/start with data retained,
no inbound network exposure (SSM Session Manager only), one-command teardown.

The lab code is shipped as a CDK S3 asset bundled from this repo at deploy
time — no dependency on the GitHub repo being public.
"""
import os
import aws_cdk as cdk
from aws_cdk import Stack, CfnOutput, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_s3_assets as s3_assets

INSTANCE_TYPE = os.environ.get("LAB_INSTANCE_TYPE", "t4g.large")  # arm64
PKG_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


class AgentTraceLabStack(Stack):
    def __init__(self, scope, cid, **kw):
        super().__init__(scope, cid, **kw)

        # The whole lab package, zipped and staged to the CDK assets bucket.
        asset = s3_assets.Asset(
            self, "LabCode",
            path=PKG_ROOT,
            # The deck is not needed on the instance; it is ~400 KB of asset
            # upload per deploy. Glob rather than filenames so a renamed deck
            # stays excluded.
            exclude=[".git", "**/__pycache__", "aws/cdk.out", "aws/.venv",
                     "*.pptx", "*.pdf"],
        )

        # Public subnet only — an IGW is free, a NAT gateway is not.
        vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=1,
            nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(
                name="public", subnet_type=ec2.SubnetType.PUBLIC)],
        )

        # No inbound rules at all. Dashboards are reached via SSM port
        # forwarding, which rides the SSM agent's outbound connection.
        sg = ec2.SecurityGroup(
            self, "Sg", vpc=vpc, allow_all_outbound=True,
            description="agent-trace-lab: no inbound; access via SSM only")

        with open(os.path.join(os.path.dirname(__file__), "user-data.sh")) as f:
            script = f.read().replace("__ASSET_S3_URL__", asset.s3_object_url)
        user_data = ec2.UserData.custom(script)

        # Note: the construct id doubles as the CFN logical id — bump the
        # suffix whenever user-data must re-run (forces instance replacement).
        instance = ec2.Instance(
            self, "Lab38",
            vpc=vpc,
            security_group=sg,
            instance_type=ec2.InstanceType(INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64),
            user_data=user_data,
            ssm_session_permissions=True,
            block_devices=[ec2.BlockDevice(
                device_name="/dev/xvda",
                volume=ec2.BlockDeviceVolume.ebs(
                    30,
                    volume_type=ec2.EbsDeviceVolumeType.GP3,
                    encrypted=True,
                    delete_on_termination=True),
            )],
        )
        asset.grant_read(instance.role)

        Tags.of(self).add("project", "opensearchcon-agent-trace-lab")

        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "StartCmd",
                  value=f"aws ec2 start-instances --instance-ids {instance.instance_id}")
        CfnOutput(self, "StopCmd",
                  value=f"aws ec2 stop-instances --instance-ids {instance.instance_id}")
        CfnOutput(self, "TunnelCmd", value=(
            "aws ssm start-session --target " + instance.instance_id +
            " --document-name AWS-StartPortForwardingSession"
            " --parameters '{\"portNumber\":[\"5602\"],\"localPortNumber\":[\"5602\"]}'"))


app = cdk.App()
AgentTraceLabStack(app, "AgentTraceLab")
app.synth()
