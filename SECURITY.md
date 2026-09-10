# Security

This repository is currently in the governance and planning stage. Keep security controls simple and proportionate to the current scope.

## Secret handling
- Never commit `.env`, `.env.*`, AWS credentials, private keys, certificates, tokens, or passwords.
- Do not store credentials in repository files, shell history, or local notes that may be accidentally pushed.
- Prefer GitHub-encrypted secrets and short-lived credentials when future workflows require them.

## Reporting security issues
- If you discover a security problem, do not open a public issue with sensitive details.
- Report it privately through the repository owner or the project maintainer channels.
- Share only the minimum information needed to assess and remediate the issue.

## Repository hygiene
- Run lightweight secret scans and repository validation before merging changes.
- Keep generated files and local artifacts out of version control.
- Do not add unnecessary infrastructure or tooling that would expand attack surface before it is required.
