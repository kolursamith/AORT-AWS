# GitHub setup and verification

## Current external status

At scaffold time the local repository had no commit or remote. The connected GitHub account was verified as `kolursamith`; a repository lookup for `kolursamith/AORT-AWS` returned `404 Not Found`, a branch lookup reported that the repository does not exist, and the teammate permission lookup returned no permission. GitHub CLI also identified `kolursamith`, but its saved token was invalid. The available connector cannot create repositories or administer rulesets, so re-authenticate the CLI first:

```bash
gh auth login -h github.com
gh auth status
gh repo view kolursamith/AORT-AWS --json nameWithOwner,visibility,defaultBranchRef,url
```

If the repository does not exist, create it only after authentication:

```bash
gh repo create kolursamith/AORT-AWS --private
git remote -v  # origin is already configured to the intended URL
```

Do not recreate it if `gh repo view` succeeds. Push the initial setup branch, review it, then establish `main`, `developer`, `feature/intelligence`, and `feature/backend` without force pushes.

## Rulesets for `main` and `developer`

In **Settings → Rules → Rulesets**, create an active branch ruleset targeting each permanent branch (or one ruleset targeting both) with:

- require pull request; one approval;
- require code-owner review and dismiss stale approvals;
- require conversation resolution;
- required checks: `quality-and-tests`, `owned-paths`, `dependency-and-secrets`;
- block force pushes and deletions;
- restrict direct updates and do not grant collaborator bypass.

Set `main` as the default branch. Invite `girinandanv9845-ship-it` with Write access, not Admin. Private-repository ruleset features vary by GitHub plan/account context; an enabled workflow is not proof that branch protection is enforced.

## API verification

After authentication, record—not assume—the results:

```bash
gh api repos/kolursamith/AORT-AWS/branches --paginate --jq '.[].name'
gh api repos/kolursamith/AORT-AWS/rulesets --jq '.[] | {id,name,enforcement,conditions,rules}'
gh api repos/kolursamith/AORT-AWS/collaborators/girinandanv9845-ship-it/permission
```

Open harmless test PRs from each feature branch to `developer`. Confirm the other teammate can approve, all three checks are required, stale approval is dismissed after a new commit, an unauthorized cross-owned path fails `owned-paths`, and direct merge/push controls are visible. Do not test protection by pushing directly to a protected branch.
