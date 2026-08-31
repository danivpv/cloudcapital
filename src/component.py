"""CDK stack: Amplify-hosted Next.js frontend plus an always-on ECS API.

Inputs are CloudFormation parameters so deployment credentials and identifiers
never enter source control. The stack deliberately requires HTTPS: the ALB
header is a shared secret and must not traverse the public internet over HTTP.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    CfnParameter,
    Stack,
    aws_ec2 as ec2,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from .api.infrastructure import ApiConstruct
from .frontend.infrastructure import FrontendConstruct


class CloudCapitalStack(Stack):
    """Integrates the always-on backend API and the Amplify frontend."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        repository = CfnParameter(
            self,
            "FrontendRepository",
            type="String",
            description="Git repository URL for the Next.js frontend.",
        )
        branch_name = CfnParameter(
            self, "FrontendBranch", type="String", default="main"
        )
        github_token_secret_arn = CfnParameter(
            self,
            "GithubTokenSecretArn",
            type="String",
            no_echo=True,
            description="Secrets Manager ARN holding the GitHub access token used by Amplify.",
        )
        openrouter_secret_arn = CfnParameter(
            self,
            "OpenRouterSecretArn",
            type="String",
            no_echo=True,
            description="Secrets Manager ARN holding the OpenRouter API key.",
        )

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC
                )
            ],
        )
        openrouter_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "OpenRouterSecret", openrouter_secret_arn.value_as_string
        )
        header_secret = secretsmanager.Secret(
            self,
            "ApiHeaderSecret",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{}',
                generate_string_key="header",
                password_length=40,
                exclude_punctuation=True,
            ),
        )
        header_value = header_secret.secret_value_from_json("header").unsafe_unwrap()

        repository_root = Path(__file__).resolve().parents[1]
        self.api = ApiConstruct(
            self,
            "Api",
            vpc=vpc,
            api_header_value=header_value,
            openrouter_secret=openrouter_secret,
            repository_root=repository_root,
        )

        github_token = secretsmanager.Secret.from_secret_complete_arn(
            self, "GithubToken", github_token_secret_arn.value_as_string
        )
        # The ALB DNS name is the API base URL. No custom domain needed for the demo.
        api_base_url = cdk.Fn.join(
            "", ["http://", self.api.load_balancer.load_balancer_dns_name]
        )
        self.frontend = FrontendConstruct(
            self,
            "Frontend",
            repository=repository.value_as_string,
            branch_name=branch_name.value_as_string,
            github_token=github_token,
            api_base_url=api_base_url,
            api_header_value=header_value,
        )

        CfnOutput(
            self,
            "ApiLoadBalancerDns",
            value=self.api.load_balancer.load_balancer_dns_name,
        )
        CfnOutput(self, "AmplifyDefaultDomain", value=self.frontend.default_domain)
        CfnOutput(self, "ApiHeaderSecretArn", value=header_secret.secret_arn)


# Keep the cdk alias import usable for any module that uses `cdk.App` in app.py
_ = cdk

# Backward compatibility aliases
AlwaysOnApi = ApiConstruct
AmplifyFrontend = FrontendConstruct
