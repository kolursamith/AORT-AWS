# Repository Structure and Ownership

Module layout for the implementation phases, following the blueprint's
recommended structure. Ownership is enforced through `.github/CODEOWNERS`
(which only takes effect once branch protection is applied — see
[branch-protection.md](branch-protection.md)).

## Modules

| Directory | Owner | Phase | Status |
|---|---|---|---|
| `banking/` | Owner B | 2 — Banking foundation | ✅ Done ([PR #1](https://github.com/kolursamith/AORT-AWS/pull/1)) |
| `telemetry/` | Owner B | 3 — Telemetry validation | Not started |
| `digital-twin/` | Owner A | 4 — Digital twin | Not started |
| `scenarios/` | Owner A (semantics) | 5 — Failure scenarios | Not started |
| `scenarios/injection/` | Owner B (injection) | 5 | Not started |
| `ai/` | Owner A | 7 — AI prediction | Not started |
| `optimizer/` | Owner A | 8 — Recovery optimizer | Not started |
| `dashboard/` | both | 9 — Decision console | Not started |
| `aws/` | Owner B | 10 — AWS deployment | Not started |
| `infrastructure/` | Owner B | cross-phase | Not started |
| `contracts/` | both | 1 — Contracts | ⏳ Pending — see [contracts/README.md](../contracts/README.md) |
| `tests/` | both | cross-phase | Not started |
| `docs/` | both | cross-phase | In use |

`banking/` is not on `develop` yet — it arrives when PR #1 merges.

## Existing Phase-I directories

`architecture/`, `dataset/`, `presentation/`, `results/` and `src/` were added
in July 2026 for the Phase-I academic submission and are unchanged here.

---

## ⚠️ Two open structural questions — joint decision needed

Neither is resolved in this PR, because both affect the shared repository
layout and are not one owner's call.

### 1. `dataset/` versus `datasets/`

The blueprint's recommended structure lists **`datasets/`** (plural). The
repository already contains **`dataset/`** (singular), holding
`dataset_description.pdf` and empty `raw/` and `processed/` directories.

**No `datasets/` directory was created here**, deliberately — adding one beside
the existing `dataset/` would leave two plausible homes for experiment data and
guarantee that files eventually land in both.

Options:

- **(a)** Keep `dataset/` and treat the blueprint's `datasets/` as a naming
  slip. Lowest churn; nothing moves. *Suggested.*
- **(b)** Rename `dataset/` → `datasets/` to match the blueprint exactly.
  Cleaner against the document, but rewrites an existing path.

Phase 6 is when this actually starts mattering.

### 2. `src/*` overlaps the new top-level modules

The repository has `src/aws/`, `src/backend/`, `src/frontend/` and
`src/ml_model/` — currently placeholder READMEs with no code. These overlap
conceptually with the top-level `aws/`, `ai/` and `dashboard/` modules from the
blueprint structure.

Nothing under `src/` was moved or deleted here.

Options:

- **(a)** Retire `src/*` as implementation lands in the top-level modules, and
  remove the placeholders once each is superseded. Keeps one structure.
  *Suggested.*
- **(b)** Keep `src/` as the code root and nest the modules inside it. Consistent
  with the Phase-I layout, but diverges from the blueprint and from where
  `banking/` already sits.

Worth settling before Phase 3 adds a second implemented module, so the
precedent is set once rather than argued twice.
