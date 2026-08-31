"""CDK construct for the Amplify-hosted Next.js frontend."""

from __future__ import annotations

from aws_cdk import (
    aws_amplify as amplify,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class FrontendConstruct(Construct):
    """Provisions the Next.js frontend on AWS Amplify Hosting (WEB_COMPUTE)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        repository: str,
        branch_name: str,
        github_token: secretsmanager.ISecret,
        api_base_url: str,
        api_header_value: str,
    ) -> None:
        super().__init__(scope, construct_id)

        build_spec = """version: 1
applications:
  - appRoot: src/frontend
    frontend:
      phases:
        preBuild:
          commands:
            - npm ci
            - echo "API_BASE_URL=$API_BASE_URL" >> .env.production
            - echo "API_SHARED_SECRET=$API_SHARED_SECRET" >> .env.production
            - echo "NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL" >> .env.production
        build:
          commands:
            - npm run build
      artifacts:
        baseDirectory: .next
        files:
          - '**/*'
      cache:
        paths:
          - node_modules/**/*
"""
        role = iam.Role(
            self,
            "AmplifyRole",
            assumed_by=iam.ServicePrincipal("amplify.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess-Amplify")
            ],
        )

        self.app = amplify.CfnApp(
            self,
            "App",
            name="cloudcapital",
            repository=repository,
            access_token=github_token.secret_value.unsafe_unwrap(),
            platform="WEB_COMPUTE",
            iam_service_role=role.role_arn,
            build_spec=build_spec,
            environment_variables=[
                amplify.CfnApp.EnvironmentVariableProperty(
                    name="AMPLIFY_MONOREPO_APP_ROOT", value="src/frontend"
                )
            ],
        )

        self.branch = amplify.CfnBranch(
            self,
            "ProductionBranch",
            app_id=self.app.attr_app_id,
            branch_name=branch_name,
            stage="PRODUCTION",
            enable_auto_build=True,
            framework="Next.js - SSR",
            environment_variables=[
                amplify.CfnBranch.EnvironmentVariableProperty(
                    name="NEXT_PUBLIC_API_URL", value="/api/backend"
                ),
                amplify.CfnBranch.EnvironmentVariableProperty(
                    name="API_BASE_URL",
                    value=api_base_url,
                ),
                amplify.CfnBranch.EnvironmentVariableProperty(
                    name="API_SHARED_SECRET",
                    value=api_header_value,
                ),
            ],
        )

        self.default_domain = self.app.attr_default_domain


# Backwards compatibility alias
AmplifyFrontend = FrontendConstruct
