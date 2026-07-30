# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| Phase-I (documentation) | ✅ Active |
| Phase-II (implementation) | 🔜 Planned |

## Reporting a Vulnerability

**AORT** is an academic disaster-recovery planning framework targeting banking ERP systems. Security is a first-class design concern.

If you discover a security vulnerability in project documentation, planned architecture, or (in Phase-II) implemented code:

1. **Do not** open a public GitHub issue for sensitive security findings
2. Email the project team or report through the course instructor
3. Include:
   - Description of the vulnerability
   - Steps to reproduce (if applicable)
   - Potential impact on banking/compliance context
   - Suggested remediation (optional)

We aim to acknowledge reports within **5 business days**.

## Security Design Principles (AORT)

| Principle | Implementation Direction |
|-----------|-------------------------|
| Least Privilege | IAM roles scoped per service; no long-lived credentials in code |
| Encryption | Data at rest (S3, RDS) and in transit (TLS via CloudFront/ALB) |
| Auditability | CloudTrail, Config, and structured recovery decision logs |
| Compliance | RBI-aligned controls, audit trails for recovery decisions |
| Secrets Management | AWS Secrets Manager / Parameter Store (Phase-II) |
| Network Isolation | VPC private subnets for ERP and twin workloads |

## Sensitive Data Handling

- **Never commit** `.env` files, AWS credentials, API keys, or real banking data
- Use synthetic datasets only (see `dataset/`)
- `.gitignore` excludes environment files and secrets by default

## Phase-I Scope

Phase-I contains **no runnable infrastructure or application code**. Security documentation describes planned controls for Phase-II implementation.
