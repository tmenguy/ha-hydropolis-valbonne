"""Shared fixtures for Hydropolis Valbonne tests."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

from dotenv import load_dotenv
import pytest

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from custom_components.hydropolis_valbonne.api import (
    DailyMeasure,
    HydropolisClient,
    HydropolisContract,
)
from custom_components.hydropolis_valbonne.const import CONF_CONTRAT_ID, DOMAIN

load_dotenv()

HYDROPOLIS_USERNAME = os.environ.get("HYDROPOLIS_USERNAME")
HYDROPOLIS_PASSWORD = os.environ.get("HYDROPOLIS_PASSWORD")
has_credentials = bool(HYDROPOLIS_USERNAME and HYDROPOLIS_PASSWORD)

live = pytest.mark.skipif(
    not has_credentials,
    reason="HYDROPOLIS_USERNAME / HYDROPOLIS_PASSWORD not set in .env",
)

FAKE_CONTRAT_ID = "9999"
FAKE_SERIAL = "ABC123456"
FAKE_EMAIL = "test@example.com"
FAKE_PASSWORD = "secret"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock, enable_custom_integrations
):
    """Enable custom integrations in all tests.

    recorder_mock must be initialized before enable_custom_integrations
    (see pytest-homeassistant-custom-component docs).
    """
    yield


@pytest.fixture
def fake_contract() -> HydropolisContract:
    return HydropolisContract(
        contrat_id=FAKE_CONTRAT_ID,
        numcontrat="C-9999",
        pconso_id="P-100",
        compteur_numserie=FAKE_SERIAL,
        actif=True,
        address="1 Rue du Test",
    )


def _make_measures(count: int = 5, start_date: date | None = None, start_index: int = 100_000) -> list[DailyMeasure]:
    """Build a list of realistic DailyMeasure objects."""
    base = start_date or date.today() - timedelta(days=count)
    measures = []
    index = start_index
    for i in range(count):
        d = base + timedelta(days=i)
        consumption = 150 + i * 10
        index += consumption
        measures.append(
            DailyMeasure(
                date=d,
                timestamp=datetime(d.year, d.month, d.day, 23, 59, 0),
                consumption_liters=consumption,
                meter_index=index,
            )
        )
    return measures


@pytest.fixture
def fake_measures() -> list[DailyMeasure]:
    return _make_measures()


@pytest.fixture
def mock_config_entry(hass: HomeAssistant):
    """Create and add a MockConfigEntry for Hydropolis."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hydropolis C-9999",
        data={
            CONF_USERNAME: FAKE_EMAIL,
            CONF_PASSWORD: FAKE_PASSWORD,
            CONF_CONTRAT_ID: FAKE_CONTRAT_ID,
            "compteur_numserie": FAKE_SERIAL,
        },
        unique_id=FAKE_CONTRAT_ID,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_hydropolis_client(fake_measures, fake_contract):
    """Patch HydropolisClient so no network calls are made.

    Returns an AsyncMock that behaves like a real HydropolisClient.
    """
    client = AsyncMock(spec=HydropolisClient)
    client.authenticate = AsyncMock(return_value=True)
    client.get_contracts = AsyncMock(return_value=[fake_contract])
    client.get_daily_measures = AsyncMock(return_value=fake_measures)
    client.data_available_since = date.today() - timedelta(days=365)
    client.invalidate_tokens = lambda: None

    with patch(
        "custom_components.hydropolis_valbonne.coordinator.HydropolisClient",
        return_value=client,
    ), patch(
        "custom_components.hydropolis_valbonne.config_flow.HydropolisClient",
        return_value=client,
    ):
        yield client


# ---------------------------------------------------------------------------
# Fixtures for multi-contract tests
# ---------------------------------------------------------------------------

FAKE_CONTRAT_ID_2 = "8888"
FAKE_SERIAL_2 = "XYZ987654"


@pytest.fixture
def fake_contract_2() -> HydropolisContract:
    return HydropolisContract(
        contrat_id=FAKE_CONTRAT_ID_2,
        numcontrat="C-8888",
        pconso_id="P-200",
        compteur_numserie=FAKE_SERIAL_2,
        actif=True,
        address="2 Rue du Test",
    )


@pytest.fixture
def fake_measures_2() -> list[DailyMeasure]:
    return _make_measures(count=3, start_date=date.today() - timedelta(days=10))


@pytest.fixture
def mock_config_entry_2(hass: HomeAssistant):
    """Second config entry — same username, different contract."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hydropolis C-8888",
        data={
            CONF_USERNAME: FAKE_EMAIL,
            CONF_PASSWORD: FAKE_PASSWORD,
            CONF_CONTRAT_ID: FAKE_CONTRAT_ID_2,
            "compteur_numserie": FAKE_SERIAL_2,
        },
        unique_id=FAKE_CONTRAT_ID_2,
    )
    entry.add_to_hass(hass)
    return entry
