"""CDK construct for Economics & Data Lambda service."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Duration,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class EconomicsConstruct(Construct):
    """Provisions the serverless Economics & Data backend Lambda."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data_bucket: s3.IBucket | None = None,
        header_secret: secretsmanager.ISecret | None = None,
        repository_root: Path | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        environment: dict[str, str] = {
            "CC_DATA_DIR": "/var/task/src/economics/data",
        }
        if data_bucket:
            environment["CC_DATA_BUCKET"] = data_bucket.bucket_name
        if header_secret:
            environment["CC_AUTH_SECRET_ARN"] = header_secret.secret_arn

        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name="/cloudcapital/economics",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        repo_root = Path(__file__).resolve().parents[2]

        self.function = _lambda.DockerImageFunction(
            self,
            "Function",
            code=_lambda.DockerImageCode.from_image_asset(
                str(repo_root),
                file="src/economics/Dockerfile",
            ),
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            timeout=Duration.seconds(30),
            environment=environment,
            log_group=log_group,
        )

        if data_bucket:
            data_bucket.grant_read(self.function)
        if header_secret:
            header_secret.grant_read(self.function)
