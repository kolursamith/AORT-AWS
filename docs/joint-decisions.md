# AORT — 8 decisions we need to make together

**Owner A (@kolursamith) · Owner B (@girinandanv9845-ship-it)** · 2026-09-09

Repo state: zero code conflicts (a dry-run merge of all Owner B work into
`main` applies cleanly, and the `dataset/`→`datasets/` rename auto-resolves).
Everything below is about **process and CI semantics**, not code.

Nothing has been merged, closed or pushed to `main`/`develop`. "Recommendation"
means Owner B's suggestion, **not** a decision taken.

---

| # | Decision | The tradeoff | Recommendation *(not a decision)* |
|---|---|---|---|
| **1** | **Is `develop` still the integration branch?** | Blueprint says `main ← develop ← feature/*`. In practice governance went straight to `main`, and `develop` is unprotected with no CI. PRs #1 and #5 target `develop`, so today they'd merge into a branch with no checks. | **Keep `develop`**, sync it from `main`, and protect it. Both open PRs already target it and merge cleanly. Alternative — drop `develop` and target `main` — is also coherent, but let's not leave it ambiguous. |
| **2** | **Are `.env.example` templates allowed?** | `.gitignore` deliberately re-includes them; PR #4's CI would reject them by filename. The two rules directly contradict. | **Allow them.** Fix is written, tested and ready: `AORT-pr4-env-regex-fix.patch`. Content-level secret scanning (gitleaks + the pattern scan) is untouched, so nothing is weakened. |
| **3** | **Should `main` require status checks to pass?** | Today the ruleset requires a PR + code-owner review but **no** status checks — which is how PR #3 merged with its own CI failing. CI is currently advisory. | **Require them, but only after decision 4 is settled** — `main`'s current CI can't pass anywhere (it demands 12 directories, 8 absent from `main`, plus a `contracts/*-schema.json` that exists nowhere). Turning on required checks first would freeze the repo. |
| **4** | **Fix `main`'s CI, or restore the missing directories?** | `main`'s CI requires 12 top-level dirs and a contract schema. 6 of the 8 missing dirs exist **only** in the closed PR #2 — so Owner A's CI currently depends on work that was closed. | **Both, in order:** merge PR #4 (it already relaxes the structure check to the Phase-I layout and makes the contract step skip when absent), then restore the module scaffolding later as real modules land. Don't reopen PR #2. |
| **5** | **Keep `require_code_owner_review` on `main`?** | **Tested empirically (PR #6, now closed).** See result below — it is *not* the clean deadlock we feared, but it does have a real consequence. | **Keep it**, and rely on the fact that with two collaborators one approval already means the other person reviewed. But see the caveat below — one sub-question is still untested. |
| **6** | **Whose `AGENTS.md`?** | Neither is live. PR #4's is 19 lines (scope, ownership, no invented telemetry, no force-push, no secrets). Closed PR #2's is 128 lines and adds branching rules, commit discipline, contract-freezing rules, evidence standards, test-integrity. | **Merge both** — PR #4's structure as the skeleton, plus PR #2's rules on evidence, contracts and not deleting tests. They're complementary, not competing. |
| **7** | **What format are contracts — JSON schema or Markdown?** | Owner A's CI validates `contracts/*-schema.json`. Owner B's two existing contract docs are Markdown proposals. No `*-schema.json` exists anywhere. | **Markdown for proposals, JSON schema once frozen.** Phase 3's output is deliberately a *proposal* with open questions; it isn't a schema yet. PR #4 already relaxes the CI to accept this. |
| **8** | **Accept `provisional-0.1` / `telemetry-observed-0.2-proposed`?** | These are the Phase 2 run-record schema and the Phase 3 observed-telemetry fields. Both explicitly marked **not frozen**, pending Owner A's review. Phase 4 (twin) and Phase 6 (dataset) both depend on them. | **Owner A to review** — 5 specific questions are listed at the end of `contracts/telemetry-observed-0.2-proposed.md` (normalization boundary, pull-vs-stream, resolution/retention, stable component identity, whether `hikaricp_*` is the right dependency signal). This is the one item that blocks Phase 4. |

---

## Decision 5 — what the test actually showed

We opened a throwaway PR (#6) touching only `banking/`, a path where CODEOWNERS
lists Owner B alone. It has been **closed unmerged and its branch deleted**.

| Observation | Result |
|---|---|
| Reviewer auto-requested for a `banking/`-only change | **Nobody** — `requested_reviewers: []` |
| Can the author approve their own PR? | **No** — `HTTP 422: "Can not approve your own pull request"` |
| PR state | `mergeable: true`, `mergeable_state: blocked`, `reviewDecision: REVIEW_REQUIRED` |
| **Control:** same PR after adding a file under `docs/` (owned by both) | **`kolursamith` auto-requested** |

The control matters: CODEOWNERS auto-request works fine. The empty request on
the `banking/`-only change was specifically because the sole code owner was
also the author.

**What this means in practice:** every `banking/`, `telemetry/`, `aws/` or
`infrastructure/` PR from Owner B will sit at *Review required* with **no
reviewer automatically requested**. Owner A has to notice it and review
unprompted — nothing will nudge them.

> ⚠️ **One sub-question we could not test alone.** Whether Owner A's approval —
> from someone who is *not* a code owner of `/banking/` — is enough to unblock
> the merge, or whether the code-owner condition stays unsatisfiable. That
> needs Owner A to actually approve a test PR. **It's a 30-second test when
> you're both available, and worth doing before relying on this setup.** If it
> turns out to block, the fix is either adding both owners to those paths or
> turning off `require_code_owner_review` and keeping the 1-approval rule.

---

## Suggested order

1. Decisions **1** and **2** (quick, unblock everything else)
2. Merge **PR #4** with the `.env.example` fix → `main`'s CI becomes passable
3. Merge **PR #1** (Phase 2 banking) → `develop`
4. Merge **PR #5** (Phase 3 telemetry) → `develop` — this creates `contracts/`
5. Run the 30-second code-owner test from decision 5
6. Sync + protect `develop`; add required status checks to `main`
7. Decision **8** — unblocks Phase 4

Full evidence and reproduction commands: `AORT-repo-reconciliation-report.md`.
