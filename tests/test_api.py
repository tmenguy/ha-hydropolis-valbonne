"""Live API tests for HydropolisClient -- zero mocks, real network calls.

Skipped entirely when HYDROPOLIS_USERNAME / HYDROPOLIS_PASSWORD are not
present in the environment (i.e. no .env file).
"""

from __future__ import annotations

from datetime import date, timedelta

import aiohttp
import pytest

import socket as socket_mod

from custom_components.hydropolis_valbonne.api import (
    HydropolisClient,
)

from .conftest import HYDROPOLIS_PASSWORD, HYDROPOLIS_USERNAME, has_credentials, live

pytestmark = [
    live,
    pytest.mark.timeout(30),
]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """Override conftest autouse fixture; live API tests need real sockets, not HA.

    Restore the real socket class and connect method so external hosts work.
    """
    import pytest_socket

    real_socket = pytest_socket._true_socket
    real_connect = pytest_socket._true_connect
    socket_mod.socket = real_socket
    socket_mod.socket.connect = real_connect
    yield


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture
async def client(session: aiohttp.ClientSession) -> HydropolisClient:
    assert HYDROPOLIS_USERNAME and HYDROPOLIS_PASSWORD
    return HydropolisClient(session, HYDROPOLIS_USERNAME, HYDROPOLIS_PASSWORD)


async def test_authenticate_success(client: HydropolisClient):
    result = await client.authenticate()
    assert result is True


async def test_authenticate_bad_password(session: aiohttp.ClientSession):
    bad_client = HydropolisClient(session, "nobody@example.com", "wrong")
    result = await bad_client.authenticate()
    assert result is False


async def test_get_contracts(client: HydropolisClient):
    await client.authenticate()
    contracts = await client.get_contracts()

    assert len(contracts) >= 1
    c = contracts[0]
    assert c.contrat_id
    assert c.compteur_numserie
    assert c.numcontrat


async def test_get_daily_measures(client: HydropolisClient):
    await client.authenticate()
    contracts = await client.get_contracts()
    c = contracts[0]

    end = date.today()
    start = end - timedelta(days=30)
    measures = await client.get_daily_measures(
        c.contrat_id, c.compteur_numserie, start, end
    )

    assert len(measures) > 0
    m = measures[0]
    assert m.date >= start
    assert m.consumption_liters >= 0
    assert m.meter_index > 0
    assert m.timestamp is not None


async def test_data_available_since(client: HydropolisClient):
    """After 3Int auth, data_available_since should be parsed from the JWT."""
    await client.authenticate()
    contracts = await client.get_contracts()
    c = contracts[0]

    end = date.today()
    start = end - timedelta(days=1)
    await client.get_daily_measures(c.contrat_id, c.compteur_numserie, start, end)

    since = client.data_available_since_for(c.contrat_id)
    assert since is not None
    assert since < date.today()


async def test_reauth_after_invalidation(client: HydropolisClient):
    """Invalidate tokens, then call get_daily_measures -- should auto-reauth."""
    await client.authenticate()
    contracts = await client.get_contracts()
    c = contracts[0]

    client.invalidate_tokens()

    end = date.today()
    start = end - timedelta(days=7)
    measures = await client.get_daily_measures(
        c.contrat_id, c.compteur_numserie, start, end
    )
    assert len(measures) > 0


@pytest.mark.timeout(60)
async def test_pagination_fetches_all_pages(client: HydropolisClient):
    """Fetch a multi-year range and verify all pages are returned.

    The API paginates at ~365 items. A 2-year range should return more
    than 365 measures, proving pagination works.
    """
    await client.authenticate()
    contracts = await client.get_contracts()
    c = contracts[0]

    end = date.today()
    start = end - timedelta(days=365 * 2)
    measures = await client.get_daily_measures(
        c.contrat_id, c.compteur_numserie, start, end
    )

    assert len(measures) > 365, (
        f"Expected >365 measures for a 2-year range, got {len(measures)}"
    )

    last = measures[-1]
    assert last.meter_index > 2_000_000, (
        f"Latest meter_index should be >2M (current reading), got {last.meter_index}"
    )
