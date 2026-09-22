"""Tests for the Hydropolis Valbonne coordinator."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from custom_components.hydropolis_valbonne.api import (
    HydropolisApiError,
    HydropolisAuthError,
)
from custom_components.hydropolis_valbonne.const import DOMAIN
from custom_components.hydropolis_valbonne.coordinator import (
    SHARED_CLIENTS_KEY,
    HydropolisCoordinator,
)

from .conftest import (
    FAKE_CONTRAT_ID,
    FAKE_CONTRAT_ID_2,
    FAKE_EMAIL,
    FAKE_PASSWORD,
    _make_measures,
)


async def _setup(hass: HomeAssistant, mock_config_entry):
    """Set up the integration via the HA config entry machinery."""
    if mock_config_entry.state is not ConfigEntryState.LOADED:
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator: HydropolisCoordinator = mock_config_entry.runtime_data
    return coordinator


async def _setup_both(hass: HomeAssistant, entry_a, entry_b):
    """Set up two config entries and return their coordinators.

    Entries may already be LOADED by the time we get here (HA auto-loads
    NOT_LOADED entries during pending-task processing), so skip ones that are.
    """
    for entry in (entry_a, entry_b):
        if entry.state is not ConfigEntryState.LOADED:
            await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry_a.runtime_data, entry_b.runtime_data


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
    coordinator_1, coordinator_2 = await _setup_both(
        hass, mock_config_entry, mock_config_entry_2
    )

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry_2.state is ConfigEntryState.LOADED

    mock_hydropolis_client.authenticate.assert_called_once()

    shared = hass.data.get(SHARED_CLIENTS_KEY, {})
    assert FAKE_EMAIL in shared
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

    async def measures_by_contract(contrat_id, serial, start, end):
        if contrat_id == FAKE_CONTRAT_ID:
            return fake_measures
        return fake_measures_2

    mock_hydropolis_client.get_daily_measures = AsyncMock(
        side_effect=measures_by_contract
    )

    coordinator_1, coordinator_2 = await _setup_both(
        hass, mock_config_entry, mock_config_entry_2
    )

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
    coordinator_1, coordinator_2 = await _setup_both(
        hass, mock_config_entry, mock_config_entry_2
    )

    assert coordinator_1.statistic_id != coordinator_2.statistic_id
    assert coordinator_1.statistic_id == f"{DOMAIN}:{FAKE_CONTRAT_ID}_water_meter"
    assert coordinator_2.statistic_id == f"{DOMAIN}:{FAKE_CONTRAT_ID_2}_water_meter"


async def test_shared_client_dropped_when_last_entry_removed(
    hass: HomeAssistant,
    mock_config_entry,
    mock_config_entry_2,
    mock_hydropolis_client,
):
    """Shared client must persist while any entry for the user remains,
    and be dropped only when the last entry is removed."""
    await _setup_both(hass, mock_config_entry, mock_config_entry_2)

    shared = hass.data[SHARED_CLIENTS_KEY]
    assert FAKE_EMAIL in shared

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert FAKE_EMAIL in shared, "client dropped too early — second entry still uses it"

    await hass.config_entries.async_remove(mock_config_entry_2.entry_id)
    await hass.async_block_till_done()
    assert FAKE_EMAIL not in shared, "client should be dropped after last entry removed"


# ---------------------------------------------------------------------------
# Authentication failures at setup
# ---------------------------------------------------------------------------


async def test_transient_api_error_retries_setup(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """A 5xx from the SSO must schedule a retry, not fail the entry for good.

    The Hydropolis SSO intermittently answers 500 to valid credentials; the
    entry has to come back on its own once the backend recovers.
    """
    mock_hydropolis_client.authenticate = AsyncMock(
        side_effect=HydropolisApiError("SSO login returned 500")
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_rejected_credentials_start_reauth_flow(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """Rejected credentials must offer the user a reauth flow."""
    mock_hydropolis_client.authenticate = AsyncMock(return_value=False)
    mock_hydropolis_client.last_auth_error = "Le mot de passe est incorrect !"

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_failed_setup_does_not_cache_client(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """A client that failed to authenticate must not be reused later."""
    mock_hydropolis_client.authenticate = AsyncMock(return_value=False)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.data.get(SHARED_CLIENTS_KEY, {}) == {}


async def test_new_password_replaces_cached_client(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """Changing the password must not keep using the stale shared client."""
    mock_hydropolis_client.credentials_match = (
        lambda username, password: password == FAKE_PASSWORD
    )

    await _setup(hass, mock_config_entry)
    assert mock_hydropolis_client.authenticate.call_count == 1

    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_PASSWORD: "new-password"},
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_hydropolis_client.authenticate.call_count == 2


async def test_auth_error_during_refresh_starts_reauth_flow(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """Credentials going stale later also triggers a reauth flow."""
    coordinator = await _setup(hass, mock_config_entry)

    mock_hydropolis_client.get_daily_measures = AsyncMock(
        side_effect=HydropolisAuthError("Failed to authenticate with Omega SSO")
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
