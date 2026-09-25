"""CDK stack: Serverless API (API Gateway + Lambdas) plus Amplify Next.js frontend."""

from __future__ import annotations

from typing import Any

from aws_cdk import (
    CfnOutput,
    CfnParameter,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from .ai.infrastructure import AiConstruct
from .economics.infrastructure import EconomicsConstruct
from .frontend.infrastructure import FrontendConstruct


class CloudCapitalStack(Stack):
    """Integrates serverless Economics and AI microservices with the Amplify frontend."""

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

        # 1. S3 Data bucket for precomputed analytical dice & parquet datasets
        self.data_bucket = s3.Bucket(
            self,
            "DataBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        # 2. Shared secret for server-to-server auth between Amplify proxy and API
        self.header_secret = secretsmanager.Secret(
            self,
            "ApiHeaderSecret",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}",
                generate_string_key="header",
                password_length=40,
                exclude_punctuation=True,
            ),
        )
        header_value = self.header_secret.secret_value_from_json(
            "header"
        ).unsafe_unwrap()

        openrouter_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "OpenRouterSecret", openrouter_secret_arn.value_as_string
        )

        # 3. Microservice Constructs
        self.economics = EconomicsConstruct(
            self,
            "Economics",
            data_bucket=self.data_bucket,
            header_secret=self.header_secret,
        )

        self.ai = AiConstruct(
            self,
            "Ai",
            openrouter_secret=openrouter_secret,
            header_secret=self.header_secret,
        )

        # 4. API Gateway REST API with Payload Response Streaming
        self.api = apigw.RestApi(
            self,
            "ApiGateway",
            rest_api_name="CloudCapitalApi",
            description="Serverless API routing to Economics and streaming AI Agent Lambdas.",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["*"],
            ),
            deploy_options=apigw.StageOptions(
                stage_name="prod",
            ),
        )

        # AI Agent routes with true response streaming. The AI container runs
        # uvicorn behind the AWS Lambda Web Adapter, which produces the
        # InvokeWithResponseStream wire format Python runtimes cannot emit
        # natively. The escape hatch sets STREAM + the streaming URI + a 60s
        # integration timeout (default 29s is short for LLM agents).
        ai_integration = apigw.LambdaIntegration(self.ai.function)
        assistant_resource = self.api.root.add_resource("assistant")
        assistant_method = assistant_resource.add_method("POST", ai_integration)

        ask_resource = self.api.root.add_resource("ask")
        ask_method = ask_resource.add_method("POST", ai_integration)

        for method in (assistant_method, ask_method):
            cfn_method = method.node.default_child
            cfn_method.add_property_override("Integration.ResponseTransferMode", "STREAM")
            cfn_method.add_property_override("Integration.TimeoutInMillis", 60_000)
            # Streaming invocation URI: routes the call through
            # InvokeWithResponseStream instead of the standard Invoke action.
            cfn_method.add_property_override(
                "Integration.Uri",
                f"arn:aws:apigateway:{self.region}:lambda:path/2021-11-15/functions/"
                + f"{self.ai.function.function_arn}/response-streaming-invocations",
            )

        # Economics & Data routes (default proxy)
        econ_integration = apigw.LambdaIntegration(self.economics.function)
        self.api.root.add_method("ANY", econ_integration)
        self.api.root.add_proxy(
            default_integration=econ_integration,
            any_method=True,
        )

        # 5. Frontend Construct (Amplify Next.js SSR)
        github_token = secretsmanager.Secret.from_secret_complete_arn(
            self, "GithubToken", github_token_secret_arn.value_as_string
        )
        self.frontend = FrontendConstruct(
            self,
            "Frontend",
            repository=repository.value_as_string,
            branch_name=branch_name.value_as_string,
            github_token=github_token,
            api_base_url=self.api.url,
            api_header_value=header_value,
        )

        # Outputs
        CfnOutput(self, "ApiGatewayUrl", value=self.api.url)
        CfnOutput(self, "DataBucketName", value=self.data_bucket.bucket_name)
        CfnOutput(self, "AmplifyDefaultDomain", value=self.frontend.default_domain)
        CfnOutput(self, "ApiHeaderSecretArn", value=self.header_secret.secret_arn)
