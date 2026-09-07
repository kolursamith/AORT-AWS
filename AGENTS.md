# AGENTS.md — Working Rules for AI Coding Agents on AORT

These rules apply to any AI coding agent (Claude Code, Codex, Copilot agents,
or similar) making changes in this repository. They also read as sensible rules
for the human owners.

AORT is a **controlled academic prototype**, not a production banking platform.
The credibility of the project rests on its results being reproducible and
honestly reported. Most of the rules below exist to protect that, not to create
bureaucracy.

---

## 1. Branching and history

- **Never work directly on `main` or `develop`.** Always use a feature branch:
  `<module>-<description>`, e.g. `banking-fineract-setup`,
  `telemetry-otel-collector`.
- **Never force-push**, and never rewrite published history.
- **Never reset or discard project history** unless the owner explicitly
  authorises that specific action.
- One focused task per branch. Delete the branch after merge.

## 2. Committing

- **Inspect `git status` and `git diff` before every commit.** Read what you are
  about to commit.
- Run `git diff --check` for whitespace damage.
- **Never run an unrestricted `git add .`** without reviewing the file list
  first. Stage deliberately.
- Write focused, conventional commits. Do not bundle unrelated changes.
- Do not commit generated run output, virtualenvs, or build artifacts.

## 3. Secrets

- **Never commit secrets, credentials, `.env` files, private keys, tokens or
  AWS access keys.** Use a `.env.example` with clearly-labelled local-only
  placeholder values instead.
- If a local development password is unavoidable in a committed template, label
  it as local-only and state that the AWS phase must use IAM / Secrets Manager.
- If you discover a committed secret, stop and report it. Do not quietly
  rewrite history to hide it.

## 4. Module ownership

- **Never modify another owner's module** unless explicitly requested, and then
  only with review by that owner. See `.github/CODEOWNERS`.
  - Owner A: `digital-twin/`, `scenarios/` (semantics), `ai/`, `optimizer/`
  - Owner B: `banking/`, `telemetry/`, `aws/`, `infrastructure/`,
    `scenarios/injection/`
  - Joint: `contracts/`, `tests/`, `docs/`, `dashboard/`, `.github/`
- **Keep changes scoped to the assigned task.** Do not opportunistically
  refactor code you happened to read.
- If a cross-module change is genuinely unavoidable, **document why in the PR
  and request review from the owning owner.** Do not slip it in silently.

## 5. Contracts

- Shared interfaces and schemas in `contracts/` are **jointly owned**.
- **Never unilaterally freeze or finalise a shared contract.** A schema marked
  provisional stays provisional until both owners agree.
- Freeze the input contract before freezing an exact dataset schema, and only
  after the real telemetry surface has been inspected.

## 6. Data and telemetry honesty

This is the most important section in this file.

- **Never invent telemetry because it appears in a diagram or a plan.** A field
  existing in a design document is not evidence that the running system emits
  it. Inspect the actual system.
- **Never fabricate banking data or telemetry and present it as real.** All
  experimental data must come from actually running the controlled system.
- **Never present simulated, placeholder or hand-written output as a measured
  result.** If something is estimated, illustrative or synthetic, label it
  clearly as such.
- Report outcomes faithfully. If a test fails, say so and show the output. If a
  step was skipped, say it was skipped.

## 7. AWS

- **Never add an AWS service just because it is available.** Every service must
  be justified against a concrete need in the PR.
- Develop and validate locally first. Cloud cost and configuration complexity
  should not consume the research implementation time.
- Flag any local-only shortcut (disabled TLS, plaintext local credentials,
  permissive networking) that must not carry over into the cloud phase.

## 8. Tests and CI

- **Never delete, skip or weaken a test to make CI pass.** Fix the underlying
  problem, or explain why the test is wrong and get it reviewed.
- Never disable a CI check to unblock a merge.
- If CI cannot run something meaningfully yet, make it skip loudly and visibly,
  not silently pass.

## 9. Evidence

- **Never declare a phase, task or fix complete without evidence.** "It should
  work" is not completion.
- Evidence means real output from a real run: test results, verification
  output, measured numbers — pasted, not paraphrased.
- Prefer verification that confirms the result **independently** of the code
  under test. Re-reading state from the system is stronger than trusting a
  script's own self-report.

## 10. Scope discipline

- Do the task that was asked. Do not silently expand it.
- Do not claim the prototype does more than it does. In particular:
  - Apache Fineract is a **representative** core-banking workload, not a
    complete bank.
  - UPI, ATM and card networks, RTGS and SWIFT are **not implemented**. They may
    appear only as dependency placeholders for future work, never as working
    features.
- If you disagree with the request, say so once, clearly — then either proceed
  as asked or stop and ask. Do not quietly do something different.

---

## Before opening a pull request

1. `git status` and `git diff` reviewed
2. No secrets staged
3. Changes scoped to the task
4. Evidence gathered for anything claimed as working
5. Every section of the PR template filled in — including "None" where it
   genuinely does not apply
