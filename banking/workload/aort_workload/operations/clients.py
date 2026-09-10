"""Customer/client operations."""

from __future__ import annotations

import random
from datetime import date

from ..bootstrap import BankingSetup
from ..client import FineractClient
from ..config import DATE_FORMAT, LOCALE

FIRST_NAMES = [
    "Aarav", "Diya", "Vihaan", "Ananya", "Arjun", "Ishita", "Kabir", "Meera",
    "Rohan", "Saanvi", "Aditya", "Priya", "Karthik", "Nisha", "Rahul", "Tara",
]
LAST_NAMES = [
    "Sharma", "Iyer", "Nair", "Patel", "Reddy", "Bose", "Menon", "Kulkarni",
    "Verma", "Rao", "Chatterjee", "Pillai",
]


def fineract_date(value: date) -> str:
    """Format a date the way Fineract's dd MMMM yyyy parser expects."""
    return value.strftime("%d %B %Y")


def create_client(
    client: FineractClient,
    setup: BankingSetup,
    rng: random.Random,
    business_date: date,
) -> int | None:
    """Onboard one active client. Returns the new client id, or None on failure."""
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    # Fineract enforces unique external ids. The run id is mixed in so that
    # re-running with the same --seed replays the same decisions without
    # colliding with clients created by an earlier run.
    external_id = f"AORT-{client.report.run_id}-{rng.randrange(10**6, 10**7)}"

    result, payload = client.post(
        "/clients",
        category="client",
        operation="client.create",
        json_body={
            "officeId": setup.office_id,
            "firstname": first,
            "lastname": last,
            "externalId": external_id,
            "legalFormId": 1,  # PERSON
            "active": True,
            "activationDate": fineract_date(business_date),
            "dateFormat": DATE_FORMAT,
            "locale": LOCALE,
        },
        context={"externalId": external_id},
    )
    if not result.ok:
        return None
    return int(payload["clientId"]) if "clientId" in payload else result.resource_id


def read_client(client: FineractClient, client_id: int) -> None:
    """Read a client back - representative of normal read-heavy API traffic."""
    client.get(
        f"/clients/{client_id}",
        category="client",
        operation="client.read",
        context={"clientId": client_id},
    )


def list_clients(client: FineractClient, limit: int = 20) -> None:
    """Paged client listing, as a UI or reporting caller would issue."""
    client.get(
        "/clients",
        category="client",
        operation="client.list",
        params={"offset": 0, "limit": limit},
    )
