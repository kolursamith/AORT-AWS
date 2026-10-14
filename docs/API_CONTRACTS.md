# API contracts

`contracts/v1/models.py` is canonical. Run `python -m contracts.v1.generate_schemas` after an intentional model change and commit all four generated schemas. CI regenerates them and fails on drift.

## Messages

- `TransactionEvent`: observable lifecycle fact. Hidden simulator truth must not be embedded in it.
- `RouteTelemetry`: source-identified, sequenced route observation with bounded measures.
- `RecoveryDecision`: expiring advisory budget and unique selected transaction IDs.
- `StatusCheckResult`: beneficiary outcome; authoritative results require a validated evidence source ID.

All IDs are bounded and pattern-checked, timestamps require an offset, unknown fields are rejected, and numeric ranges are explicit. Version `1.0` is frozen in the v1 models. Breaking changes require a new contract directory and compatibility tests.

## HTTP surface

| Service | Endpoint | Behavior |
|---|---|---|
| Controller | `GET /health` | Liveness and controller mode |
| Controller | `POST /v1/recovery/decisions` | Conservative advisory decision |
| Remitter | `POST /v1/payments` | Synthetic request accepted; outcome remains unknown |
| Switch | `POST /v1/status-checks` | Quota/credit gated; unknown until evidence exists |
| Beneficiary | `GET /v1/mock/status/{id}` | Deterministic synthetic evidence for isolated tests |
