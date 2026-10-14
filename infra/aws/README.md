# AWS deployment gate

No infrastructure is provisioned by this repository. The 48-hour MVP target is one EC2 host running the Compose stack, CloudWatch Agent for container logs, and a private S3 bucket for synthetic results. Before provisioning, review estimated cost, least-privilege IAM, security groups, encrypted storage, backups, teardown, and a no-real-data policy.

The course prototype may later move stateless services to ECS Fargate, PostgreSQL to RDS, and asynchronous check dispatch to SQS. DynamoDB is not justified unless a measured twin-state access pattern requires it. SageMaker is optional and is not required for the MVP.
