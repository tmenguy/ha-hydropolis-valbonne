"""Tests for the Hydropolis Valbonne coordinator."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.hydropolis_valbonne.api import HydropolisApiError
from custom_components.hydropolis_valbonne.const import DOMAIN
from custom_components.hydropolis_valbonne.coordinator import HydropolisCoordinator

from .conftest import FAKE_CONTRAT_ID, _make_measures


async def _setup(hass: HomeAssistant, mock_config_entry):
    """Set up the integration via the HA config entry machinery."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator: HydropolisCoordinator = mock_config_entry.runtime_data
    return coordinator


async def test_first_refresh_fetches_full_history(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """On the very first run, data_available_since is used as start date."""
    coordinator = await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert coordinator.data is not None
    assert coordinator.data.meter_total_liters > 0
    assert coordinator.data.last_measurement is not None
    mock_hydropolis_client.get_daily_measures.assert_called()


async def test_no_measures_first_run_loads_gracefully(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """If no measures come back on the first refresh, entry still loads.

    The API legitimately returns no data when there are no new measures.
    The sensor will show 'unknown' until data arrives, but the
    integration should not go into SETUP_RETRY.
    """
    mock_hydropolis_client.get_daily_measures = AsyncMock(return_value=[])

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    coordinator: HydropolisCoordinator = mock_config_entry.runtime_data
    assert coordinator.data is None


async def test_no_new_measures_keeps_previous(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """When subsequent refresh returns no new data, previous data is kept."""
    coordinator = await _setup(hass, mock_config_entry)
    prev_data = coordinator.data

    mock_hydropolis_client.get_daily_measures = AsyncMock(return_value=[])
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data is prev_data


async def test_api_error_raises_update_failed(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    coordinator = await _setup(hass, mock_config_entry)

    mock_hydropolis_client.get_daily_measures = AsyncMock(
        side_effect=HydropolisApiError("server down")
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False


async def test_statistic_id_is_external(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """The statistic_id should be an external-source ID (domain:identifier)."""
    coordinator = await _setup(hass, mock_config_entry)

    stat_id = coordinator.statistic_id
    assert stat_id == f"{DOMAIN}:{FAKE_CONTRAT_ID}_water_meter"
    assert stat_id.startswith(f"{DOMAIN}:")


async def test_incremental_refresh(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """After initial import, second refresh should still work with new data."""
    coordinator = await _setup(hass, mock_config_entry)

    new_measures = _make_measures(count=1, start_date=date.today())
    mock_hydropolis_client.get_daily_measures = AsyncMock(return_value=new_measures)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data is not None
    assert coordinator.data.meter_total_liters == new_measures[-1].meter_index


# ---------------------------------------------------------------------------
# Multi-contract tests
# ---------------------------------------------------------------------------

from .conftest import FAKE_CONTRAT_ID_2, FAKE_EMAIL


async def test_two_contracts_share_single_client(
    hass: HomeAssistant,
    mock_config_entry,
    mock_config_entry_2,
    mock_hydropolis_client,
):
    """Two config entries for the same user must reuse one HydropolisClient.

    authenticate() must be called exactly once — not once per entry — so
    that the second coordinator's login does not invalidate the first
    entry's Omega SSO session.
    """
    from custom_components.hydropolis_valbonne.coordinator import SHARED_CLIENTS_KEY

    from homeassistant.config_entries import ConfigEntryState
    if mock_config_entry.state is not ConfigEntryState.LOADED:
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
    if mock_config_entry_2.state is not ConfigEntryState.LOADED:
        await hass.config_entries.async_setup(mock_config_entry_2.entry_id)
    await hass.async_block_till_done()

    from homeassistant.config_entries import ConfigEntryState

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry_2.state is ConfigEntryState.LOADED

    # Only one authenticate() call across both entries
    mock_hydropolis_client.authenticate.assert_called_once()

    # Both entries reference the exact same client object in hass.data
    shared = hass.data.get(SHARED_CLIENTS_KEY, {})
    assert FAKE_EMAIL in shared
    coordinator_1: HydropolisCoordinator = mock_config_entry.runtime_data
    coordinator_2: HydropolisCoordinator = mock_config_entry_2.runtime_data
    assert coordinator_1._client is coordinator_2._client


async def test_two_contracts_independent_data(
    hass: HomeAssistant,
    mock_config_entry,
    mock_config_entry_2,
    mock_hydropolis_client,
    fake_measures,
    fake_measures_2,
):
    """Each coordinator returns its own data even when sharing a client."""
    from unittest.mock import AsyncMock
    from custom_components.hydropolis_valbonne.const import CONF_CONTRAT_ID

    # Return different measures depending on which contrat_id is requested
    async def measures_by_contract(contrat_id, serial, start, end):
        if contrat_id == FAKE_CONTRAT_ID:
            return fake_measures
        return fake_measures_2

    mock_hydropolis_client.get_daily_measures = AsyncMock(
        side_effect=measures_by_contract
    )

    from homeassistant.config_entries import ConfigEntryState
    if mock_config_entry.state is not ConfigEntryState.LOADED:
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
    if mock_config_entry_2.state is not ConfigEntryState.LOADED:
        await hass.config_entries.async_setup(mock_config_entry_2.entry_id)
    await hass.async_block_till_done()

    coordinator_1: HydropolisCoordinator = mock_config_entry.runtime_data
    coordinator_2: HydropolisCoordinator = mock_config_entry_2.runtime_data

    assert coordinator_1.data.meter_total_liters == fake_measures[-1].meter_index
    assert coordinator_2.data.meter_total_liters == fake_measures_2[-1].meter_index
    assert coordinator_1.data.meter_total_liters != coordinator_2.data.meter_total_liters


async def test_second_contract_has_distinct_statistic_id(
    hass: HomeAssistant,
    mock_config_entry,
    mock_config_entry_2,
    mock_hydropolis_client,
):
    """Each contract must have a unique statistic_id for the Energy dashboard."""
    from custom_components.hydropolis_valbonne.const import DOMAIN

    from homeassistant.config_entries import ConfigEntryState
    if mock_config_entry.state is not ConfigEntryState.LOADED:
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
    if mock_config_entry_2.state is not ConfigEntryState.LOADED:
        await hass.config_entries.async_setup(mock_config_entry_2.entry_id)
    await hass.async_block_till_done()

    coordinator_1: HydropolisCoordinator = mock_config_entry.runtime_data
    coordinator_2: HydropolisCoordinator = mock_config_entry_2.runtime_data

    assert coordinator_1.statistic_id != coordinator_2.statistic_id
    assert coordinator_1.statistic_id == f"{DOMAIN}:{FAKE_CONTRAT_ID}_water_meter"
    assert coordinator_2.statistic_id == f"{DOMAIN}:{FAKE_CONTRAT_ID_2}_water_meter"
