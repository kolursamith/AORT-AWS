"""Offline validation of the AWS prototype template.

No AWS account is involved: cfn-lint plus structural checks. That is the limit
of what can be verified without deploying, and the README says so plainly.

The security assertions matter most. This stack would hold a banking
prototype's backups, so an accidentally public bucket or an open SSH port is a
defect, not a preference.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cfnlint import api
from cfnlint.decode import cfn_yaml

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "cloudformation" / "aort-prototype.yml"


@pytest.fixture(scope="module")
def template():
    # cfn-lint 1.x returns the decoded template directly.
    return cfn_yaml.load(str(TEMPLATE_PATH))


@pytest.fixture(scope="module")
def raw():
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def resources_of(template, resource_type):
    return {name: body for name, body in template["Resources"].items()
            if body["Type"] == resource_type}


# --- the template is valid CloudFormation -----------------------------------------

def test_cfn_lint_reports_no_errors_or_warnings(raw):
    matches = api.lint_all(raw)
    serious = [m for m in matches if str(m.rule.id)[0] in {"E", "W"}]
    assert not serious, "\n".join(f"{m.rule.id} {m.message}" for m in serious)


def test_template_declares_required_sections(template):
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    for section in ("Description", "Parameters", "Resources", "Outputs"):
        assert section in template


# --- only the services classified CORE --------------------------------------------

def test_only_core_services_are_provisioned(template):
    # EC2 to run the stack, S3 for backups, IAM for access, CloudWatch for
    # AWS-side telemetry. Anything else was classified as deferred.
    allowed = {
        "AWS::EC2::Instance", "AWS::EC2::SecurityGroup",
        "AWS::S3::Bucket", "AWS::S3::BucketPolicy",
        "AWS::IAM::Role", "AWS::IAM::InstanceProfile",
        "AWS::Logs::LogGroup",
    }
    used = {body["Type"] for body in template["Resources"].values()}
    assert used <= allowed, f"unexpected services: {sorted(used - allowed)}"


@pytest.mark.parametrize("deferred", [
    "AWS::RDS::DBInstance", "AWS::Lambda::Function", "AWS::StepFunctions::StateMachine",
    "AWS::DynamoDB::Table", "AWS::SageMaker::NotebookInstance", "AWS::Cognito::UserPool",
])
def test_deferred_services_are_absent(template, deferred):
    assert not resources_of(template, deferred)


# --- security ----------------------------------------------------------------------

def test_no_security_group_is_open_to_the_world(template, raw):
    for name, group in resources_of(template, "AWS::EC2::SecurityGroup").items():
        for rule in group["Properties"].get("SecurityGroupIngress", []):
            assert rule.get("CidrIp") != "0.0.0.0/0", f"{name} is open to the internet"
    assert "0.0.0.0/0" not in raw.replace("0.0.0.0/0 ", ""), "world-open CIDR present"


def test_ssh_access_must_be_set_deliberately(template):
    parameter = template["Parameters"]["AllowedSshCidr"]
    assert parameter.get("Default") != "0.0.0.0/0"
    assert "AllowedPattern" in parameter


def test_backup_bucket_blocks_all_public_access(template):
    buckets = resources_of(template, "AWS::S3::Bucket")
    assert buckets, "no backup bucket"
    for name, bucket in buckets.items():
        block = bucket["Properties"]["PublicAccessBlockConfiguration"]
        assert all(block[key] is True for key in (
            "BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"
        )), f"{name} does not block public access"


def test_backup_bucket_is_encrypted_and_versioned(template):
    for name, bucket in resources_of(template, "AWS::S3::Bucket").items():
        assert "BucketEncryption" in bucket["Properties"], f"{name} is unencrypted"
        assert bucket["Properties"]["VersioningConfiguration"]["Status"] == "Enabled"


def test_iam_policies_are_not_wildcards(template):
    for name, role in resources_of(template, "AWS::IAM::Role").items():
        for policy in role["Properties"].get("Policies", []):
            for statement in policy["PolicyDocument"]["Statement"]:
                actions = statement["Action"]
                actions = actions if isinstance(actions, list) else [actions]
                assert "*" not in actions, f"{name} grants every action"
                resource = statement["Resource"]
                resources = resource if isinstance(resource, list) else [resource]
                if any(r == "*" for r in resources):
                    # Only metric publishing legitimately needs Resource "*",
                    # and then only for this project's namespace.
                    assert actions == ["cloudwatch:PutMetricData"], (
                        f"{name} uses Resource '*' for {actions}")
                    assert "Condition" in statement, f"{name} must scope PutMetricData"


def test_no_secrets_or_credentials_are_embedded(raw):
    patterns = [r"AKIA[0-9A-Z]{16}", r"aws_secret_access_key", r"BEGIN [A-Z ]*PRIVATE KEY",
                r"(?i)password\s*[:=]\s*['\"][^'\"]{3,}"]
    for pattern in patterns:
        assert not re.search(pattern, raw), f"template matches {pattern}"


# --- cost control -------------------------------------------------------------------

def test_log_retention_is_bounded(template):
    groups = resources_of(template, "AWS::Logs::LogGroup")
    assert groups, "no log group"
    for name, group in groups.items():
        assert group["Properties"].get("RetentionInDays", 0) > 0, f"{name} retains forever"


def test_bucket_expires_old_versions(template):
    for name, bucket in resources_of(template, "AWS::S3::Bucket").items():
        rules = bucket["Properties"]["LifecycleConfiguration"]["Rules"]
        assert any(r.get("Status") == "Enabled" for r in rules), f"{name} never expires data"


def test_instance_type_is_a_parameter_with_a_small_default(template):
    parameter = template["Parameters"]["InstanceType"]
    assert parameter["Default"].startswith("t3."), "default should be a burstable instance"
    assert "AllowedValues" in parameter, "constrain the size so cost cannot surprise"


# --- operability ---------------------------------------------------------------------

def test_outputs_tell_the_operator_what_they_need(template):
    outputs = template["Outputs"]
    assert {"BackupBucketName", "InstanceId", "SshTunnelCommand"} <= set(outputs)


def test_user_data_script_exists_and_is_referenced(raw):
    script = TEMPLATE_PATH.parents[1] / "scripts" / "user-data.sh"
    assert script.exists(), "bootstrap script missing"
    assert "docker" in raw.lower(), "template does not install or run docker"
