# Contributing

## Development workflow
1. Create a feature branch from `develop` for the task you are working on.
2. Keep the change limited to the intended task and related validation.
3. Run the relevant repository checks before opening a pull request.
4. Open a pull request against `develop` for peer review.
5. Resolve review feedback before merging.
6. Merge approved work into `develop` and later promote to `main` when the branch is stable.

## Pull request expectations
- Explain the summary, motivation, and files changed.
- Note any contract, telemetry, AWS, or dataset impact.
- Include validation evidence and any screenshots or logs needed.
- Check that no secrets, credentials, or unrelated files are included.

## Governance expectations
- Respect module ownership and cross-owner review for shared files.
- Keep repository governance simple and phase-appropriate.
- Do not invent future implementation details or add unnecessary infrastructure before it is needed.
