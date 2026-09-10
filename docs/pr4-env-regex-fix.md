# PR #4 — proposed fix for the `.env.example` CI rule

**For:** @kolursamith · **From:** Owner B · **Target:** PR #4, branch `agents/pasted-text-processing-f52d47e2`
**Status:** Proposed diff — **not pushed.** Ready-to-apply patch:
`AORT-pr4-env-regex-fix.patch`

> I did not push this to your branch. It is your PR and your branch, and the
> change is small enough to review inline. Apply the patch or paste the block
> below, whichever you prefer.

---

## The problem

PR #4's *Check for committed env or credential files* step matches on
**filenames**. Its pattern includes `(^|/)\.env\.`, which matches
`.env.example` as well as `.env`.

Verified by extracting the step from PR #4's own `ci.yml` and running it
verbatim against the real file list of `telemetry-otel-prometheus`:

```
=== PR #4 CURRENT (unpatched) step vs telemetry branch ===
Environment or credential files must not be committed.
  -> FAIL (exit 1)

=== files it objects to ===
    banking/.env.example
    telemetry/.env.example
```

Both files are deliberate. They are the only documentation of the
configuration each stack needs, both READMEs instruct `cp .env.example .env`,
and `.gitignore` explicitly re-includes them while ignoring the real `.env`:

```gitignore
!.env.example
!*.env.example
```

So `.gitignore` and this CI rule currently contradict each other. As it
stands, PR #1 and PR #5 would fail CI on files the repository is deliberately
configured to keep.

Note this only bites once PR #4 merges — `main`'s current check is anchored
(`^\.env`) and does not match a nested `banking/.env.example`.

## The fix

Filter template suffixes out of the filename match. Everything else is
unchanged.

```yaml
      - name: Check for committed env or credential files
        shell: bash
        run: |
          set -euo pipefail
          # Committed *templates* are deliberate and must be allowed. banking/
          # and telemetry/ each ship a .env.example that is the only
          # documentation of the configuration those stacks need, and both
          # READMEs instruct `cp .env.example .env`. The repository .gitignore
          # explicitly re-includes them while ignoring the real .env files.
          # File *contents* remain covered by the secret-pattern scan above and
          # by gitleaks in secret-scan.yml, so this exemption does not weaken
          # secret detection - it only stops filename matching from rejecting
          # legitimate templates.
          matches="$(git ls-files \
            | grep -E '(^|/)(\.env|\.env\.|credentials|.*credentials.*|.*\.pem$|.*\.key$|.*\.p12$|.*\.pfx$|.*\.crt$|.*\.jks$)' \
            | grep -vE '\.(example|sample|template)$' || true)"
          if [ -n "$matches" ]; then
            echo 'Environment or credential files must not be committed:'
            printf '  %s\n' $matches
            exit 1
          fi
          echo 'No .env or credential files tracked (templates such as *.env.example are allowed).'
```

Two incidental improvements: it now **prints which files matched** (the
original said only "must not be committed"), and `|| true` keeps it safe under
`set -o pipefail`.

## Does this weaken secret detection?

No. This step only ever matched **filenames**. File **contents** are still
covered by two other checks that are untouched:

- the *Secret pattern scan* step in the same job (AWS keys, private keys)
- `gitleaks` in `secret-scan.yml`

If someone did put a real secret inside a `.env.example`, those still catch it.
What changes is only that a filename ending in `.example` no longer fails the
build by itself.

## Verification

The patched step was extracted from the patched YAML and executed verbatim
against throwaway git repos seeded with each real file list:

```
  telemetry branch (2 .env.example)      expect=PASS got=PASS OK
  main                                   expect=PASS got=PASS OK
  PR #4 branch                           expect=PASS got=PASS OK
  real banking/.env present              expect=FAIL got=FAIL OK
        Environment or credential files must not be committed:
          banking/.env
  aws credentials file present           expect=FAIL got=FAIL OK
        Environment or credential files must not be committed:
          infrastructure/aws/credentials
```

Templates pass; a genuine `.env` or `credentials` file still fails the build.

Also confirmed: the patched `ci.yml` parses as valid YAML, and
`git apply --check` against `agents/pasted-text-processing-f52d47e2` reports
the patch applies cleanly.

## To apply

```bash
git checkout agents/pasted-text-processing-f52d47e2
git apply AORT-pr4-env-regex-fix.patch
git commit -am "ci: allow committed .env.example templates in env-file check"
git push
```

CI on PR #4 currently passes and will continue to pass after this change — that
branch has no `.env.example` files of its own, so the fix is forward-looking:
it stops PR #1 and PR #5 failing once they carry those templates in.

## One caveat about verifying on PR #4 itself

A green CI run on PR #4 after this patch does **not** by itself prove the fix
works, because PR #4's branch contains no `.env.example` files to trigger the
rule. The meaningful proof is the verbatim before/after run above, and a CI run
on PR #1 or PR #5 once PR #4 has merged.
