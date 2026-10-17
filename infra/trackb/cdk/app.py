#!/usr/bin/env python3
"""CDK entry point.  cdk synth            (core stack)
cdk synth -c addons=1 (adds EventBridge/Lambda, Step Functions campaign)"""

import os

import aws_cdk as cdk
from aorts_stack import AortsStack

app = cdk.App()
AortsStack(
    app,
    "AortsTrackB",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-south-1"),
    ),
    description="AORTS Track B academic prototype (synthetic mock banking environment)",
)
cdk.Tags.of(app).add("project", "aorts")
cdk.Tags.of(app).add("environment", "academic-dev")
app.synth()
