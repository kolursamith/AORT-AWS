# Contributing to AORT

Thank you for contributing to **AORT: Autonomous Operational Recovery Twin**. This document defines how our two-member academic team collaborates on Phase-I and future implementation phases.

## Team Structure

| Member | Branch | Primary Domains |
|--------|--------|-----------------|
| Student 1 | `feature/student1` | AWS Architecture, Digital Twin, Cloud Infrastructure, GitHub, Documentation, Frontend Planning, Papers 1–8 |
| Student 2 | `feature/student2` | AI, Recovery Optimizer, Scenario Generator, Backend Planning, Dataset, Testing, Papers 9–15 |

Both members contribute to documentation, reviews, testing planning, and presentation.

## Branch Workflow

1. **Never commit directly to `main`.**
2. Create or checkout your feature branch from `develop`:
   ```bash
   git checkout develop
   git pull origin develop
   git checkout feature/student1   # or feature/student2
   ```
3. Make focused commits with [Conventional Commits](https://www.conventionalcommits.org/) messages.
4. Push your branch and open a Pull Request to `develop`.
5. Request review from the other team member.
6. After approval, squash-merge (or merge) into `develop`.
7. Release merges: `develop` → `release/phase1` → `main` with tags.

See [github/branch-strategy/git-workflow.md](github/branch-strategy/git-workflow.md) for full details.

## Commit Message Convention

```
<type>(<scope>): <short description>

[optional body]
```

**Types:** `docs`, `arch`, `feat`, `fix`, `test`, `chore`, `refactor`

**Examples:**
- `docs(literature): add gap analysis for papers 1-3`
- `arch(aws): update VPC subnet diagram`
- `docs(dataset): define synthetic ERP schema`

## Pull Request Guidelines

- Link related issues or milestone tasks
- Include a clear summary of changes
- Add screenshots for diagram updates
- Ensure markdown renders correctly on GitHub
- Minimum **one approving review** before merge

## Documentation Standards

- Use Markdown for all documentation
- Prefer Mermaid for diagrams (GitHub-renderable)
- Cite research papers with author, title, year, and venue
- Do not copy research gaps verbatim from source papers
- Keep Phase-I free of implementation code unless explicitly approved for Phase-II

## Expected Activity (Per Student)

- 20–30 meaningful commits over the project duration
- At least 2 Pull Requests
- Active participation in code/documentation reviews
- Weekly progress updates in `docs/weekly-progress/`

## Getting Help

- Team meetings: document in `docs/meeting-notes/`
- Architecture questions: discuss in PR comments or team meetings
- Course instructor: Dr. Priya V — BCSE355L Cloud Architecture Design
