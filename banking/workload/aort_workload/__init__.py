"""AORT Phase 2 - Apache Fineract workload generator.

Drives a locally running Apache Fineract instance through its REST APIs to
produce representative core-banking activity: client onboarding, savings and
deposit accounts, loan origination and servicing, transactions, and
general-ledger entries.

Every recorded result comes from an actual Fineract HTTP response. Nothing in
this package fabricates banking data or telemetry.
"""

__version__ = "0.1.0"
