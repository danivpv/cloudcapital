"""CDK construct for AI Agent Lambda service."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Duration,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class AiConstruct(Construct):
    """Provisions the serverless AI Agent Lambda with streaming capabilities."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        openrouter_secret: secretsmanager.ISecret | None = None,
        header_secret: secretsmanager.ISecret | None = None,
        repository_root: Path | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        environment: dict[str, str] = {
            "CC_DATA_DIR": "/var/task/src/economics/data",
            # Lambda Web Adapter: API Gateway STREAM integrations invoke via
            # InvokeWithResponseStream — tell the adapter to emit that format.
            # (Local SAM/docker runs leave the image default: buffered.)
            "AWS_LWA_INVOKE_MODE": "response_streaming",
        }
        if openrouter_secret:
            environment["CC_OPENROUTER_SECRET_ARN"] = openrouter_secret.secret_arn
        if header_secret:
            environment["CC_AUTH_SECRET_ARN"] = header_secret.secret_arn

        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name="/cloudcapital/ai",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        repo_root = Path(__file__).resolve().parents[2]

        self.function = _lambda.DockerImageFunction(
            self,
            "Function",
            code=_lambda.DockerImageCode.from_image_asset(
                str(repo_root),
                file="src/ai/Dockerfile",
            ),
            architecture=_lambda.Architecture.ARM_64,
            memory_size=1024,
            timeout=Duration.seconds(60),
            environment=environment,
            log_group=log_group,
        )

        if openrouter_secret:
            openrouter_secret.grant_read(self.function)
        if header_secret:
            header_secret.grant_read(self.function)
