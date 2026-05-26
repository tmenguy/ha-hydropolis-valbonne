"""Unit tests for get_contracts() numserie resolution (no network, no credentials)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from custom_components.hydropolis_valbonne.api import HydropolisClient


def _make_contracts_response() -> dict:
    return {
        "data": [
            {
                "type": "IClient_Contrat",
                "id": "18344",
                "attributes": {"contrat_id": "18344", "pconso_id": "pconso_A", "numcontrat": "10002878", "actif": "1"},
                "relationships": {"pconso": {"data": {"type": "IClient_Pconso", "id": "pconso_A"}}},
            },
            {
                "type": "IClient_Contrat",
                "id": "18343",
                "attributes": {"contrat_id": "18343", "pconso_id": "pconso_B", "numcontrat": "10002877", "actif": "1"},
                "relationships": {"pconso": {"data": {"type": "IClient_Pconso", "id": "pconso_B"}}},
            },
        ],
        "included": [
            {
                "type": "IClient_Pconso", "id": "pconso_A",
                "attributes": {"pconso_id": "pconso_A", "compteur_id": "cpt_A", "cpltadr": ""},
                "relationships": {
                    "compteur": {"data": {"type": "IClient_Compteur", "id": "cpt_A"}},
                    "pdessadr": {"data": {"type": "IClient_Pdessadr", "id": "addr_A"}},
                },
            },
            {
                "type": "IClient_Pconso", "id": "pconso_B",
                "attributes": {"pconso_id": "pconso_B", "compteur_id": "cpt_B", "cpltadr": ""},
                "relationships": {
                    "compteur": {"data": {"type": "IClient_Compteur", "id": "cpt_B"}},
                    "pdessadr": {"data": {"type": "IClient_Pdessadr", "id": "addr_B"}},
                },
            },
            {
                "type": "IClient_Compteur", "id": "cpt_A",
                "attributes": {"compteur_id": "cpt_A", "numserie": "SERIAL_A"},
                "relationships": {},
            },
            {
                "type": "IClient_Compteur", "id": "cpt_B",
                "attributes": {"compteur_id": "cpt_B", "numserie": "SERIAL_B"},
                "relationships": {},
            },
            {
                "type": "IClient_Pdessadr", "id": "addr_A",
                "attributes": {"libvoie": "1 Rue Alpha"},
                "relationships": {},
            },
            {
                "type": "IClient_Pdessadr", "id": "addr_B",
                "attributes": {"libvoie": "2 Rue Beta"},
                "relationships": {},
            },
        ],
    }


async def test_get_contracts_resolves_distinct_numserie_per_contract():
    """Each contract must resolve its own numserie via pconso, not share the first."""
    session = MagicMock()
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=_make_contracts_response())
    session.get = AsyncMock(return_value=resp)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    client = HydropolisClient(session, "user@example.com", "password")
    client._omega_token = "fake_token"

    contracts = await client.get_contracts()
    by_id = {c.contrat_id: c for c in contracts}

    assert len(contracts) == 2
    assert by_id["18344"].compteur_numserie == "SERIAL_A"
    assert by_id["18343"].compteur_numserie == "SERIAL_B"
    assert by_id["18344"].compteur_numserie != by_id["18343"].compteur_numserie


async def test_get_contracts_resolves_distinct_addresses_per_contract():
    """Each contract must resolve its own address via pconso→pdessadr."""
    session = MagicMock()
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=_make_contracts_response())
    session.get = AsyncMock(return_value=resp)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    client = HydropolisClient(session, "user@example.com", "password")
    client._omega_token = "fake_token"

    contracts = await client.get_contracts()
    by_id = {c.contrat_id: c for c in contracts}

    assert by_id["18344"].address == "1 Rue Alpha"
    assert by_id["18343"].address == "2 Rue Beta"
