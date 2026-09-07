# Branch Protection — Proposed Settings

> **⚠️ A repository admin must apply these. They are not applied yet.**
>
> Owner B (`@girinandanv9845-ship-it`) has `push` but **not** `admin` on this
> repository, so cannot configure branch protection. `@kolursamith` is the only
> admin and must apply the settings below.
>
> Until then, **all five branches are unprotected** and `.github/CODEOWNERS`
> has no effect — CODEOWNERS only routes review when protection requires it.

This repository is public, so branch protection is available on GitHub Free.

---

## Read this before applying: the two-person CODEOWNERS trap

There is a real deadlock risk in combining **"Require review from Code Owners"**
with a `CODEOWNERS` file that assigns some paths to a **single** owner.

GitHub does not let an author's own approval satisfy the code-owner
requirement. So if Owner B opens a PR touching `banking/` — a path owned solely
by Owner B — then:

- Owner B cannot approve their own PR, and
- Owner A is not a code owner of `banking/`, so their approval does not satisfy
  the code-owner rule either.

The PR can end up **permanently blocked**.

### Recommendation

For a two-person team, prefer **"Require 1 approving review"** *without*
"Require review from Code Owners".

With only two collaborators, one approval already guarantees the other person
reviewed it — you get the two-person review the blueprint asks for, without the
deadlock. `CODEOWNERS` still does its real job: automatically requesting review
from the right owner.

Enable code-owner review later if more collaborators join, or if every path in
`CODEOWNERS` gets at least two owners.

Both variants are given below. **Option A is recommended.**

---

## `main` — release branch

| Setting | Value |
|---|---|
| Require a pull request before merging | ✅ Yes |
| Required approving reviews | **1** |
| Dismiss stale approvals on new commits | ✅ Yes |
| Require review from Code Owners | ❌ No (see trap above) |
| Require status checks to pass | ✅ Yes |
| Required checks | `Static checks`, `Banking workload verification` |
| Require branches to be up to date before merging | ✅ Yes |
| Require conversation resolution | ✅ Yes |
| Allow force pushes | ❌ No |
| Allow deletions | ❌ No |
| Include administrators | ✅ Yes |

## `develop` — integration branch

| Setting | Value |
|---|---|
| Require a pull request before merging | ✅ Yes |
| Required approving reviews | **0** (PR required, no approval gate) |
| Require status checks to pass | ✅ Yes |
| Required checks | `Static checks`, `Banking workload verification` |
| Require branches to be up to date before merging | ✅ Yes |
| Allow force pushes | ❌ No |
| Allow deletions | ❌ No |
| Include administrators | ✅ Yes |

Setting required approvals to `0` on `develop` still **forces the pull-request
flow and blocks direct commits** — it just does not gate on a human approval,
which matches the blueprint's "PR + CI" for `develop` versus "PR + review + CI"
for `main`.

---

## Applying via the GitHub UI

**Settings → Branches → Add branch protection rule**, once per branch, using
the tables above.

---

## Applying via `gh` CLI

Run as an admin. These match Option A (recommended).

### `main`

```bash
gh api -X PUT repos/kolursamith/AORT-AWS/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Static checks", "Banking workload verification"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "required_conversation_resolution": true,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

### `develop`

```bash
gh api -X PUT repos/kolursamith/AORT-AWS/branches/develop/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Static checks", "Banking workload verification"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

### Option B — with code-owner review (accepts the deadlock risk)

Identical, but set `"require_code_owner_reviews": true` on `main`. Only choose
this knowing single-owner paths such as `banking/` may block. If you do, give
every `CODEOWNERS` path both owners.

---

## Ordering note

Apply protection **after** the CI workflow has run at least once on a PR.

GitHub can only require status checks it has already seen by name. If you add
`Static checks` / `Banking workload verification` as required checks before any
run has reported them, merges may hang waiting for checks that never arrive.

Suggested order:

1. Merge the governance PR (adds the CI workflow).
2. Let CI run once so GitHub learns both check names.
3. Apply the protection rules above.

---

## Verifying afterwards

```bash
gh api repos/kolursamith/AORT-AWS/branches/main/protection \
  --jq '{checks: .required_status_checks.contexts,
         reviews: .required_pull_request_reviews.required_approving_review_count,
         force_push: .allow_force_pushes.enabled,
         deletions: .allow_deletions.enabled}'
```

Expected: both checks listed, `reviews: 1`, `force_push: false`,
`deletions: false`.
