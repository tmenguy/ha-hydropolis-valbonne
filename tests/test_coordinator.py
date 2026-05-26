"""Tests for the Hydropolis Valbonne coordinator."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.hydropolis_valbonne.api import DailyMeasure, HydropolisApiError
from custom_components.hydropolis_valbonne.const import DOMAIN
from custom_components.hydropolis_valbonne.coordinator import (
    SHARED_CLIENTS_KEY,
    HydropolisCoordinator,
)

from .conftest import (
    FAKE_CONTRAT_ID,
    FAKE_CONTRAT_ID_2,
    FAKE_EMAIL,
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
# Issue #5: meter swap must not produce negative consumption
# ---------------------------------------------------------------------------


def _measure(d: date, consumption: int, index: int) -> DailyMeasure:
    return DailyMeasure(
        date=d,
        timestamp=datetime(d.year, d.month, d.day, 23, 59, 0),
        consumption_liters=consumption,
        meter_index=index,
    )


async def test_meter_swap_does_not_produce_negative_sum(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """When the physical meter is replaced, meter_index drops from a large
    value to a small one and the API reports a hugely negative consumption
    for that day. The cumulative sum recorded in statistics must NOT drop
    — the meter-swap day adds 0 to the sum (issue #5)."""
    d0 = date.today() - timedelta(days=3)
    swap_measures = [
        _measure(d0, consumption=200, index=2_500_000),
        _measure(d0 + timedelta(days=1), consumption=-2_499_800, index=200),
        _measure(d0 + timedelta(days=2), consumption=150, index=350),
    ]
    mock_hydropolis_client.get_daily_measures = AsyncMock(return_value=swap_measures)

    with patch(
        "custom_components.hydropolis_valbonne.coordinator.async_add_external_statistics"
    ) as mock_add_stats:
        await _setup(hass, mock_config_entry)

    assert mock_add_stats.called, "statistics should have been imported"
    _, _, stats = mock_add_stats.call_args[0]

    sums = [s["sum"] for s in stats]
    states = [s["state"] for s in stats]

    for i in range(1, len(sums)):
        assert sums[i] >= sums[i - 1], (
            f"sum dropped at index {i}: {sums[i]} < {sums[i - 1]}"
        )

    assert sums == [2_500_000.0, 2_500_000.0, 2_500_150.0]
    # state mirrors the corrected cumulative sum so HA's History graph
    # stays monotonic too — the raw meter reading remains accessible
    # via the sensor entity's native_value.
    assert states == sums


async def test_incremental_sum_continues_from_last_recorded(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """On an incremental refresh, the running sum must continue from the
    previously recorded statistic — not restart from the new measure's
    meter_index. This is what makes the meter-swap fix work across
    coordinator restarts."""
    d0 = date.today() - timedelta(days=2)
    first_batch = [_measure(d0, consumption=200, index=2_500_000)]
    mock_hydropolis_client.get_daily_measures = AsyncMock(return_value=first_batch)

    await _setup(hass, mock_config_entry)

    second_batch = [
        _measure(d0 + timedelta(days=1), consumption=-2_499_800, index=200),
        _measure(d0 + timedelta(days=2), consumption=150, index=350),
    ]
    coordinator: HydropolisCoordinator = mock_config_entry.runtime_data
    mock_hydropolis_client.get_daily_measures = AsyncMock(return_value=second_batch)

    with patch(
        "custom_components.hydropolis_valbonne.coordinator.async_add_external_statistics"
    ) as mock_add_stats:
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    _, _, stats = mock_add_stats.call_args[0]
    sums = [s["sum"] for s in stats]

    assert sums == [2_500_000.0, 2_500_150.0], (
        "incremental fetch must seed from last recorded sum (2_500_000), "
        f"clip the negative day, then add 150 — got {sums}"
    )
