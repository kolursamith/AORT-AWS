"""AORTS Track B - small, cost-conscious AWS deployment (academic prototype).

Essentials: VPC, ECS Fargate services (Cloud Map discovery), RDS PostgreSQL (private),
DynamoDB twin state, SQS + DLQ, S3 results, CloudWatch logs/alarms, Secrets Manager.
Add-ons (enabled by context flags, deploy after the core works): EventBridge + Lambda
archive, Step Functions experiment campaign, AWS Budget alert.

Security posture: no public database; tasks in private subnets; only the remitter's
payment API is behind the ALB and the ALB only admits ``AllowedCidr``; lab/attack and
fault controls are disabled in every AWS service; least-privilege task roles.
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import (
    CfnCondition,
    CfnOutput,
    CfnParameter,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # repository root

# name, port (None = worker), command
SERVICES = [
    (
        "switch",
        8110,
        [
            "uvicorn",
            "--factory",
            "backend.aorts.banking.switch.main:build",
            "--host",
            "0.0.0.0",
            "--port",
            "8110",
        ],
    ),
    (
        "beneficiary-b",
        8121,
        [
            "uvicorn",
            "--factory",
            "backend.aorts.banking.beneficiary.main:build",
            "--host",
            "0.0.0.0",
            "--port",
            "8121",
        ],
    ),
    (
        "beneficiary-c",
        8122,
        [
            "uvicorn",
            "--factory",
            "backend.aorts.banking.beneficiary.main:build",
            "--host",
            "0.0.0.0",
            "--port",
            "8122",
        ],
    ),
    (
        "remitter",
        8100,
        [
            "uvicorn",
            "--factory",
            "backend.aorts.banking.remitter.main:build",
            "--host",
            "0.0.0.0",
            "--port",
            "8100",
        ],
    ),
    (
        "control",
        8130,
        [
            "uvicorn",
            "--factory",
            "backend.aorts.control.main:build",
            "--host",
            "0.0.0.0",
            "--port",
            "8130",
        ],
    ),
    ("dispatcher", None, ["python", "-m", "backend.aorts.banking.dispatcher.service"]),
]


class AortsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        addons = bool(self.node.try_get_context("addons"))

        allowed_cidr = CfnParameter(
            self,
            "AllowedCidr",
            type="String",
            default="127.0.0.1/32",
            description="CIDR allowed to reach the payment API ALB (e.g. your IP/32).",
        )
        alarm_email = CfnParameter(
            self,
            "AlarmEmail",
            type="String",
            default="",
            description="Optional e-mail for alarm + budget notifications.",
        )
        budget_usd = CfnParameter(self, "MonthlyBudgetUsd", type="Number", default=30)
        has_email = CfnCondition(
            self,
            "HasEmail",
            expression=Fn.condition_not(Fn.condition_equals(alarm_email.value_as_string, "")),
        )

        # ---------------------------------------------------------------- network
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="app", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
            ],
        )
        app_sg = ec2.SecurityGroup(
            self, "AppSg", vpc=vpc, description="AORTS ECS tasks", allow_all_outbound=True
        )
        app_sg.add_ingress_rule(app_sg, ec2.Port.tcp_range(8100, 8130), "service-to-service")
        db_sg = ec2.SecurityGroup(
            self,
            "DbSg",
            vpc=vpc,
            description="RDS: only from AORTS tasks",
            allow_all_outbound=False,
        )
        db_sg.add_ingress_rule(app_sg, ec2.Port.tcp(5432), "postgres from tasks")
        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=vpc, description="ALB: only AllowedCidr")
        alb_sg.add_ingress_rule(
            ec2.Peer.ipv4(allowed_cidr.value_as_string), ec2.Port.tcp(80), "allowed client CIDR"
        )
        app_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(8100), "ALB to remitter")

        # ---------------------------------------------------------------- data
        db = rds.DatabaseInstance(
            self,
            "Postgres",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.of("16.4", "16")
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[db_sg],
            credentials=rds.Credentials.from_generated_secret("aorts"),
            database_name="aorts",
            allocated_storage=20,
            storage_encrypted=True,
            multi_az=False,
            publicly_accessible=False,
            backup_retention=Duration.days(1),
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
        )
        app_secret = sm.Secret(
            self,
            "AppSecrets",
            description="AORTS service HMAC keys + admin token (replace service_keys after deploy)",
            generate_secret_string=sm.SecretStringGenerator(
                secret_string_template=json.dumps(
                    {"service_keys": "REPLACE_WITH_scripts/generate_env.py_OUTPUT"}
                ),
                generate_string_key="admin_token",
                exclude_punctuation=True,
                password_length=40,
            ),
        )
        twin_table = ddb.Table(
            self,
            "TwinState",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        dlq = sqs.Queue(
            self,
            "DispatchDlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        queue = sqs.Queue(
            self,
            "DispatchQueue",
            visibility_timeout=Duration.seconds(30),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=dlq),
        )
        bucket = s3.Bucket(
            self,
            "Results",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
        )

        # ---------------------------------------------------------------- compute
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)
        cluster.add_default_cloud_map_namespace(name="aorts.local")
        image = ecs.ContainerImage.from_asset(
            str(PROJECT_ROOT),
            file="infra/trackb/Dockerfile",
            exclude=[".venv", "results", "infra/trackb/cdk/cdk.out", ".env", ".git"],
        )
        common_env = {
            "AORTS_ENVIRONMENT": "aws",
            "AORTS_ENABLE_ATTACK_CONTROLS": "false",
            "AORTS_ENABLE_FAULT_CONTROLS": "false",
            "AORTS_REMITTER_URL": "http://remitter.aorts.local:8100",
            "AORTS_SWITCH_URL": "http://switch.aorts.local:8110",
            "AORTS_BENEFICIARY_URLS": "B=http://beneficiary-b.aorts.local:8121,C=http://beneficiary-c.aorts.local:8122",
            "AORTS_CONTROL_URL": "http://control.aorts.local:8130",
            "AORTS_DB_NAME": "aorts",
            "AORTS_QUEUE_BACKEND": "sqs",
            "AORTS_SQS_QUEUE_URL": queue.queue_url,
            "AORTS_TWIN_STORE": "dynamodb",
            "AORTS_DYNAMODB_TABLE": twin_table.table_name,
            "AORTS_SWITCH_CHECK_WORKERS": "8",
            "AORTS_AWS_REGION": self.region,
            "AORTS_EMF_ENABLED": "true",
            "AORTS_S3_RESULTS_BUCKET": bucket.bucket_name,
            "AORTS_EVENT_SINK": "eventbridge" if addons else "local",
        }
        db_secret = db.secret
        common_secrets = {
            "AORTS_DB_HOST": ecs.Secret.from_secrets_manager(db_secret, "host"),
            "AORTS_DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
            "AORTS_DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
            "AORTS_SERVICE_KEYS": ecs.Secret.from_secrets_manager(app_secret, "service_keys"),
            "AORTS_ADMIN_TOKEN": ecs.Secret.from_secrets_manager(app_secret, "admin_token"),
        }

        def task_def(
            name: str, command: list[str], port: int | None, extra_env: dict | None = None
        ):
            td = ecs.FargateTaskDefinition(self, f"Td-{name}", cpu=256, memory_limit_mib=512)
            log_group = logs.LogGroup(
                self,
                f"Logs-{name}",
                log_group_name=f"/aorts/{name}",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
            env = {**common_env, "AORTS_SERVICE_NAME": name, **(extra_env or {})}
            container = td.add_container(
                name,
                image=image,
                command=command,
                environment=env,
                secrets=common_secrets,
                logging=ecs.LogDrivers.aws_logs(stream_prefix=name, log_group=log_group),
            )
            if port:
                container.add_port_mappings(ecs.PortMapping(container_port=port))
            return td, log_group

        services: dict[str, ecs.FargateService] = {}
        log_groups: dict[str, logs.LogGroup] = {}
        for name, port, command in SERVICES:
            extra = {"AORTS_BANK_ID": name[-1].upper()} if name.startswith("beneficiary") else {}
            td, lg = task_def(name, command, port, extra)
            log_groups[name] = lg
            services[name] = ecs.FargateService(
                self,
                f"Svc-{name}",
                cluster=cluster,
                task_definition=td,
                desired_count=1,
                security_groups=[app_sg],
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                assign_public_ip=False,
                cloud_map_options=ecs.CloudMapOptions(name=name) if port else None,
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
                min_healthy_percent=0,
            )
        migrate_td, _ = task_def(
            "migrate",
            ["sh", "-c", "alembic upgrade head && python -m backend.aorts.app.seed"],
            None,
        )
        experiments_td, _ = task_def(
            "experiments",
            ["python", "-m", "backend.aorts.experiments.run_poisoning"],
            None,
            {
                "AORTS_ENABLE_ATTACK_CONTROLS": "true",
                "AORTS_ENABLE_FAULT_CONTROLS": "true",
                "AORTS_RESULTS_DIR": "/tmp/results",
            },
        )

        # ---------------------------------------------------------------- least-privilege grants
        twin_table.grant_read_write_data(services["control"].task_definition.task_role)
        twin_table.grant_read_write_data(services["dispatcher"].task_definition.task_role)
        queue.grant_send_messages(services["dispatcher"].task_definition.task_role)
        queue.grant_consume_messages(services["dispatcher"].task_definition.task_role)
        bucket.grant_put(services["control"].task_definition.task_role)
        bucket.grant_put(experiments_td.task_role)

        # ---------------------------------------------------------------- controlled entry point
        alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", vpc=vpc, internet_facing=True, security_group=alb_sg
        )
        listener = alb.add_listener(
            "Http",
            port=80,
            open=False,
            default_action=elbv2.ListenerAction.fixed_response(
                404, content_type="text/plain", message_body="not found"
            ),
        )
        listener.add_targets(
            "Payments",
            port=8100,
            protocol=elbv2.ApplicationProtocol.HTTP,
            priority=10,
            conditions=[
                elbv2.ListenerCondition.path_patterns(["/api/v1/payments*", "/api/v1/health"])
            ],
            targets=[services["remitter"]],
            health_check=elbv2.HealthCheck(path="/api/v1/health", interval=Duration.seconds(15)),
        )

        # ---------------------------------------------------------------- monitoring
        topic = sns.Topic(self, "Alarms")
        sub = sns.CfnSubscription(
            self,
            "AlarmEmailSub",
            protocol="email",
            topic_arn=topic.topic_arn,
            endpoint=alarm_email.value_as_string,
        )
        sub.cfn_options.condition = has_email
        action = cw_actions.SnsAction(topic)

        def alarm(name: str, metric: cw.IMetric, threshold: float, periods: int = 1):
            a = cw.Alarm(
                self,
                name,
                metric=metric,
                threshold=threshold,
                evaluation_periods=periods,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            a.add_alarm_action(action)

        alarm(
            "DlqNotEmpty",
            dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(1)),
            0,
        )
        alarm(
            "DispatchBacklogOld",
            queue.metric_approximate_age_of_oldest_message(period=Duration.minutes(1)),
            120,
            3,
        )
        alarm(
            "Alb5xx",
            alb.metrics.http_code_target(
                elbv2.HttpCodeTarget.TARGET_5XX_COUNT, period=Duration.minutes(5)
            ),
            10,
        )
        alarm(
            "AlbLatency",
            alb.metrics.target_response_time(period=Duration.minutes(5), statistic="p95"),
            2.0,
            2,
        )
        alarm("RdsCpu", db.metric_cpu_utilization(period=Duration.minutes(5)), 80, 2)
        for name, svc in services.items():
            alarm(f"Cpu-{name}", svc.metric_cpu_utilization(period=Duration.minutes(5)), 85, 2)
        # Service failure: the remitter target behind the ALB is unhealthy (other services fail
        # health checks inside ECS and are restarted; the deployment circuit breaker rolls back).
        alarm(
            "RemitterUnhealthy",
            cw.Metric(
                namespace="AWS/ApplicationELB",
                metric_name="UnHealthyHostCount",
                dimensions_map={"LoadBalancer": alb.load_balancer_full_name},
                statistic="Maximum",
                period=Duration.minutes(1),
            ),
            0,
            3,
        )
        # EMF metrics emitted by the switch / services (namespace AORTS, dimension Service).
        alarm(
            "StatusCheckFlood",
            cw.Metric(
                namespace="AORTS",
                metric_name="check_rejected_total",
                dimensions_map={"Service": "switch"},
                statistic="Sum",
                period=Duration.minutes(1),
            ),
            300,
            2,
        )
        alarm(
            "AuthFailures",
            cw.Metric(
                namespace="AORTS",
                metric_name="auth_failures_total",
                dimensions_map={"Service": "remitter"},
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            20,
        )

        budget = budgets.CfnBudget(
            self,
            "Budget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=budget_usd.value_as_number, unit="USD"
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="FORECASTED",
                        threshold=80,
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL", address=alarm_email.value_as_string
                        )
                    ],
                )
            ],
        )
        budget.cfn_options.condition = has_email

        # ---------------------------------------------------------------- add-ons (deploy after the core works)
        if addons:
            bus = events.EventBus(self, "Bus", event_bus_name="aorts-bus")
            bus.grant_put_events_to(services["control"].task_definition.task_role)
            archive_fn = lambda_.Function(
                self,
                "ArchiveEvents",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="archive_events.handler",
                code=lambda_.Code.from_asset(str(Path(__file__).parent / "lambda")),
                timeout=Duration.seconds(10),
                environment={"BUCKET": bucket.bucket_name},
                log_retention=logs.RetentionDays.ONE_WEEK,
            )
            bucket.grant_put(archive_fn)
            events.Rule(
                self,
                "ArchiveRule",
                event_bus=bus,
                event_pattern=events.EventPattern(source=["aorts.trackb"]),
                targets=[targets.LambdaFunction(archive_fn)],
            )

            def run(name: str, module: str, args: list[str]):
                return tasks.EcsRunTask(
                    self,
                    f"Run-{name}",
                    integration_pattern=sfn.IntegrationPattern.RUN_JOB,
                    cluster=cluster,
                    task_definition=experiments_td,
                    assign_public_ip=False,
                    security_groups=[app_sg],
                    subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                    launch_target=tasks.EcsFargateLaunchTarget(
                        platform_version=ecs.FargatePlatformVersion.LATEST
                    ),
                    container_overrides=[
                        tasks.ContainerOverride(
                            container_definition=experiments_td.default_container,
                            command=["python", "-m", module, *args],
                        )
                    ],
                )

            campaign = (
                run(
                    "Replay",
                    "backend.aorts.experiments.run_replay",
                    ["--mode", "inprocess", "--repeats", "5"],
                )
                .next(
                    run("Poisoning", "backend.aorts.experiments.run_poisoning", ["--repeats", "5"])
                )
                .next(
                    run(
                        "Flooding",
                        "backend.aorts.experiments.run_flooding",
                        ["--mode", "inprocess", "--repeats", "3"],
                    )
                )
            )
            sfn.StateMachine(
                self,
                "ExperimentCampaign",
                definition_body=sfn.DefinitionBody.from_chainable(campaign),
                timeout=Duration.hours(2),
            )

        CfnOutput(self, "AlbDns", value=alb.load_balancer_dns_name)
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "MigrateTaskDef", value=migrate_td.task_definition_arn)
        CfnOutput(self, "AppSubnets", value=",".join(s.subnet_id for s in vpc.private_subnets))
        CfnOutput(self, "AppSecurityGroup", value=app_sg.security_group_id)
        CfnOutput(self, "AppSecretArn", value=app_secret.secret_arn)
        CfnOutput(self, "ResultsBucket", value=bucket.bucket_name)
        CfnOutput(self, "DispatchQueueUrl", value=queue.queue_url)
        CfnOutput(self, "TwinTable", value=twin_table.table_name)
