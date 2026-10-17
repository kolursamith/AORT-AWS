# Track B: technical evidence pack

This folder records **what Track B actually implements, how it works, and what was measured**,
so the material can support technical write-ups, including patent-oriented ones.

> **No patentability claim is made.** Patentability (novelty, inventive step, prior art) can
> only be judged by a qualified patent professional after a prior-art search. Nothing here is
> legal advice. Every statement is classified as one of:
>
> - **Implemented and Tested**: code exists, and automated tests or experiments exercise it.
> - **Implemented but Not Tested**: code exists, but nothing verifies it automatically.
> - **Partial**: part of the idea is implemented; the gap is stated.
> - **Proposed**: an idea only, with no implementation.
> - **Not Implemented**: in scope, but not built.

| File | Content |
|---|---|
| [01-architecture-and-algorithm-flow.md](01-architecture-and-algorithm-flow.md) | Components, data flow, and the per-tick recovery algorithm, step by step |
| [02-feature-to-code-map.md](02-feature-to-code-map.md) | Each technical feature → module(s) → tests → status |
| [03-experimental-evidence.md](03-experimental-evidence.md) | Measured effects, with seeds, CIs and caveats; nothing extrapolated |
| [04-result-files-and-hashes.md](04-result-files-and-hashes.md) | Every result file used, with its SHA-256 and the code version |
| [05-limitations-and-gaps.md](05-limitations-and-gaps.md) | What does not work, is unverified, or is out of scope |

Scope note: the brief originally assigned the route twin, transaction twin and recovery
optimizer to Track A. They were later requested explicitly as Track B additions and are
implemented in Track B as self-contained modules (`backend/aorts/twin/`, `backend/aorts/recovery/`) behind the
existing controller interface. Track A's own controller can still replace them through
`AORTS_CONTROLLER_MODE=http`. No Track A code was written or modified.
