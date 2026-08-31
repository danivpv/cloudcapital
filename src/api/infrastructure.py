"""CDK construct for the always-on Cloud Capital API service (ECS + ALB)."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Duration,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class ApiConstruct(Construct):
    """One Fargate task behind a header-gated HTTP ALB listener.

    HTTP is intentional for this demo: the x-demo-auth header gates the API
    without requiring a certificate or custom domain. A production deployment
    can switch the listener to HTTPS by adding a certificate parameter.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        api_header_value: str,
        openrouter_secret: secretsmanager.ISecret,
        repository_root: Path,
    ) -> None:
        super().__init__(scope, construct_id)

        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name="/cloudcapital/api",
            retention=logs.RetentionDays.ONE_MONTH,
        )
        task = ecs.FargateTaskDefinition(self, "Task", cpu=1024, memory_limit_mib=2048)
        container = task.add_container(
            "ApiContainer",
            image=ecs.ContainerImage.from_asset(
                str(repository_root),
                file="src/api/Dockerfile",
                exclude=[
                    ".git",
                    ".venv",
                    ".cache",
                    "cdk.out",
                    "src/frontend/node_modules",
                    "src/frontend/.next",
                ],
            ),
            logging=ecs.LogDrivers.aws_logs(log_group=log_group, stream_prefix="api"),
            environment={"CC_DATA_DIR": "/app/data"},
            secrets={
                "CC_OPENROUTER_API_KEY": ecs.Secret.from_secrets_manager(
                    openrouter_secret
                )
            },
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8080/health')\"",
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(10),
                # 5 retries × 30s interval = 2m 30s of grace after start_period.
                # The first parquet scan (dice build) can take ~20s; this gives
                # ample runway before ECS marks the task unhealthy.
                retries=5,
                start_period=Duration.seconds(60),
            ),
        )
        container.add_port_mappings(ecs.PortMapping(container_port=8080))

        # Public subnets avoid a NAT Gateway for this demo. The task security
        # group accepts inbound traffic only from the ALB; outbound is needed
        # for OpenRouter. A production private-subnet deployment adds NAT/VPC egress.
        self.service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task,
            desired_count=1,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            health_check_grace_period=Duration.seconds(60),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self, "LoadBalancer", vpc=vpc, internet_facing=True
        )
        # HTTP on port 80 — no certificate or custom domain required for the demo.
        # The x-demo-auth header still prevents unauthenticated access.
        # Default action is 403 so direct browser requests to the ALB DNS are blocked.
        self.listener = self.load_balancer.add_listener(
            "HttpListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=True,
            default_action=elbv2.ListenerAction.fixed_response(
                status_code=403,
                content_type="application/json",
                message_body='{"error":"forbidden"}',
            ),
        )
        self.target_group = self.listener.add_targets(
            "HeaderAuthenticatedApi",
            priority=10,
            conditions=[
                elbv2.ListenerCondition.http_header("x-demo-auth", [api_header_value])
            ],
            protocol=elbv2.ApplicationProtocol.HTTP,
            port=8080,
            targets=[self.service],
            health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200"),
            deregistration_delay=Duration.seconds(30),
        )
        self.service.connections.allow_from(self.load_balancer, ec2.Port.tcp(8080))


# Backwards compatibility alias
AlwaysOnApi = ApiConstruct
