<!--
AORT pull request template.

Every section below is required. If a section does not apply, write "None"
and say why in one line - do not delete the heading. The point is that the
reviewer can see the question was considered, not skipped.
-->

## Scope

<!-- What this PR changes, and which phase/module it belongs to. Keep it to
     one focused task; unrelated changes belong in their own PR. -->

## Tests run

<!-- What you actually ran, and the result. Paste real output, not a claim.
     A phase is not done without evidence - see AGENTS.md.
     If nothing was run, say so explicitly and explain why. -->

## Contracts touched

<!-- Any shared interface, schema or data format this PR adds or changes.

     Contracts are JOINTLY owned. If this PR changes one, both owners must
     agree before merge - do not treat a schema as frozen because it appears
     in a PR. If it touches nothing shared, write "None". -->

## Telemetry impact

<!-- New/changed metrics, logs, traces or exporters, and anything downstream
     collection would need to know. If this PR only *enables* a telemetry
     surface without consuming it, say that. -->

## AWS impact

<!-- Any AWS service added, changed or newly depended on, and why it is
     justified. Never add a service just because it is available.
     Also flag any local-only shortcut that must NOT carry over to the cloud
     phase (for example disabled TLS or local development credentials). -->

## Dataset impact

<!-- Whether this changes how experiment data is produced, labelled or
     stored, and whether any committed dataset is affected.
     Data must come from real controlled runs - never fabricated. -->

---

## Checklist

- [ ] Branch is a feature branch — not a direct commit to `main` or `develop`
- [ ] `git status` and `git diff` were reviewed before committing (no blind `git add .`)
- [ ] No secrets, credentials, `.env` files, keys or tokens are committed
- [ ] Changes are scoped to this task; no unrelated edits bundled in
- [ ] No other owner's module was modified without a request and review
- [ ] No tests were deleted or weakened to make CI pass
- [ ] Evidence is included above for anything claimed as working

<!-- Cross-module change? Explain below why it was unavoidable and tag the
     owning reviewer explicitly. -->
