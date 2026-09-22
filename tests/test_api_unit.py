"""Unit tests for get_contracts() numserie resolution (no network, no credentials)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date

from custom_components.hydropolis_valbonne.api import (
    HydropolisApiError,
    HydropolisAuthError,
    HydropolisClient,
)


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


# ---------------------------------------------------------------------------
# authenticate() status handling
# ---------------------------------------------------------------------------


def _make_signin_client(status: int, body: dict | None = None) -> HydropolisClient:
    """Build a client whose SSO signin answers with the given status/body."""
    session = MagicMock()
    resp = AsyncMock()
    resp.status = status
    resp.headers = {}
    resp.json = AsyncMock(return_value=body if body is not None else {})
    session.post = AsyncMock(return_value=resp)
    return HydropolisClient(session, "user@example.com", "password")


WRONG_PASSWORD_BODY = {
    "errors": [
        {
            "code": "10300002",
            "title": "Le mot de passe est incorrect !",
            "detail": "Le mot de passe est incorrect !",
        }
    ]
}


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422])
async def test_authenticate_returns_false_when_credentials_rejected(status: int):
    """The SSO rejecting the login/password pair is reported as False, not raised.

    The JVS Omega SSO uses 409 (not 401) for a wrong password.
    """
    client = _make_signin_client(status, WRONG_PASSWORD_BODY)

    assert await client.authenticate() is False
    assert client.last_auth_error == "Le mot de passe est incorrect !"


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_authenticate_raises_on_transient_error(status: int):
    """A server-side hiccup must not be reported as invalid credentials.

    The Hydropolis SSO intermittently answers 500 to perfectly valid
    credentials; treating that as a wrong password used to kill the config
    entry permanently.
    """
    client = _make_signin_client(status)

    with pytest.raises(HydropolisApiError):
        await client.authenticate()


async def test_credentials_match():
    client = HydropolisClient(MagicMock(), "user@example.com", "password")

    assert client.credentials_match("user@example.com", "password")
    assert not client.credentials_match("user@example.com", "other")
    assert not client.credentials_match("other@example.com", "password")


# ---------------------------------------------------------------------------
# 3Int token exchange
# ---------------------------------------------------------------------------


def _jwt(datedeb: str = "2020-01-01") -> str:
    import base64, json

    payload = base64.urlsafe_b64encode(
        json.dumps({"datedeb": datedeb}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


async def test_3int_401_refreshes_the_omega_session_instead_of_failing():
    """A stale Omega session must be renewed, not reported as bad credentials.

    The 3Int exchange answers 401 when the Omega session behind it expired;
    surfacing that as an auth failure would prompt the user for a password
    that is perfectly valid.
    """
    session = MagicMock()

    signin = AsyncMock()
    signin.status = 201
    signin.headers = {"authorization": "fresh-omega-token"}
    session.post = AsyncMock()

    refused = AsyncMock()
    refused.status = 401
    accepted = AsyncMock()
    accepted.status = 200
    accepted.json = AsyncMock(return_value={"token": _jwt()})

    # 3Int refuses the first exchange, the client logs in again, second works.
    session.post.side_effect = [refused, signin, accepted]

    client = HydropolisClient(session, "user@example.com", "password")
    client._omega_token = "stale-omega-token"

    await client._ensure_3int_token("9999", "SERIAL")

    assert client._3int_tokens["9999"] == _jwt()
    assert client.data_available_since_for("9999") == date(2020, 1, 1)
    assert session.post.await_count == 3


async def test_3int_401_raises_when_reauthentication_is_also_refused():
    """If the fresh login is rejected too, the credentials really are wrong."""
    session = MagicMock()

    refused = AsyncMock()
    refused.status = 401
    signin_refused = AsyncMock()
    signin_refused.status = 409
    signin_refused.headers = {}
    signin_refused.json = AsyncMock(return_value=WRONG_PASSWORD_BODY)
    session.post = AsyncMock(side_effect=[refused, signin_refused])

    client = HydropolisClient(session, "user@example.com", "password")
    client._omega_token = "stale-omega-token"

    with pytest.raises(HydropolisAuthError, match="Le mot de passe est incorrect"):
        await client._ensure_3int_token("9999", "SERIAL")
