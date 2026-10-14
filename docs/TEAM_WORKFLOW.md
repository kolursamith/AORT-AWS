# Team workflow

```text
feature/intelligence --PR--> developer --PR--> main
feature/backend ------PR--/
```

`kolursamith` owns intelligence and results. `girinandanv9845-ship-it` owns backend, dashboard, and infrastructure. Contracts, contract tests, GitHub policy, docs, and root configuration are shared. The author opens the PR; the other person performs the single required independent approval and code-owner review. Changed commits dismiss stale approval.

Do not import another owner's internal package. Integrate via `contracts/v1`, HTTP, and synthetic fixtures. Before requesting review, run `ruff format --check .`, `ruff check .`, schema generation/drift check, and `pytest`.

CODEOWNERS cannot prevent local edits. The ownership job enforces strict paths when the PR enters a protected branch. Renames check both old and new names; deletions remain visible through the GitHub changed-files API; unknown authors fail closed on strict paths.
