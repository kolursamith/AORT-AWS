# AGENTS.md

## Scope and repository expectations
- Inspect the repository before making changes.
- Keep work task-scoped and do not broaden scope without clear necessity.
- Respect module ownership and do not modify another owner's area without explicit instruction.
- Prefer incremental validation over complex new infrastructure.

## Repository rules
- Do not invent telemetry, AWS services, datasets, or implementation details that are not already present in the repository.
- Do not add unnecessary AWS services, ML infrastructure, or deployment complexity during bootstrap phases.
- Do not rewrite git history, force-push, or delete teammate work.
- Do not commit secrets, credentials, private keys, or environment files.
- Do not ship unrelated changes or fabricated data.

## Validation before completion
- Run the smallest relevant validation commands for the edited behavior.
- Check for repository-level issues such as YAML syntax, JSON validity, and secret exposure before finishing.
- Keep changes professional, maintainable, and consistent with the current Phase-I governance stage.
