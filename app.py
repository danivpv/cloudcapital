#!/usr/bin/env python3
"""CDK application entry point."""

from __future__ import annotations

import os

import aws_cdk as cdk

from src.component import CloudCapitalStack

app = cdk.App()
CloudCapitalStack(
    app,
    "CloudCapitalStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)
app.synth()
