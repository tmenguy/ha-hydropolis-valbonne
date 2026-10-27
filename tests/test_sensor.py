"""Tests for the Hydropolis Valbonne sensor entity."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant

from custom_components.hydropolis_valbonne.api import HydropolisApiError
from custom_components.hydropolis_valbonne.const import DOMAIN

from .conftest import FAKE_CONTRAT_ID


async def _get_sensor_state(hass: HomeAssistant, mock_config_entry):
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return hass.states.get(f"sensor.hydropolis_{FAKE_CONTRAT_ID}_water_meter")


async def test_sensor_state_and_attributes(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
    fake_measures,
):
    state = await _get_sensor_state(hass, mock_config_entry)
    assert state is not None

    expected_value = fake_measures[-1].meter_index
    assert int(float(state.state)) == expected_value

    attrs = state.attributes
    assert "last_measurement" in attrs
    assert attrs["last_measurement"] == fake_measures[-1].timestamp.isoformat()


async def test_sensor_device_class(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    state = await _get_sensor_state(hass, mock_config_entry)
    assert state is not None
    assert state.attributes["device_class"] == SensorDeviceClass.WATER


async def test_sensor_no_state_class(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """state_class is intentionally omitted to prevent HA from auto-generating
    statistics that would conflict with our async_import_statistics calls."""
    state = await _get_sensor_state(hass, mock_config_entry)
    assert state is not None
    assert "state_class" not in state.attributes


async def test_sensor_unit(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    state = await _get_sensor_state(hass, mock_config_entry)
    assert state is not None
    assert state.attributes["unit_of_measurement"] == UnitOfVolume.LITERS


async def test_sensor_unique_id(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
):
    """The entity_id should be based on the device name, not DOMAIN."""
    state = await _get_sensor_state(hass, mock_config_entry)
    assert state is not None
    assert state.entity_id == f"sensor.hydropolis_{FAKE_CONTRAT_ID}_water_meter"
    assert DOMAIN not in state.entity_id.replace("sensor.", "")


async def test_sensor_stays_available_when_backend_is_down(
    hass: HomeAssistant,
    mock_config_entry,
    mock_hydropolis_client,
    fake_measures,
):
    """A backend outage must not turn the meter into 'unavailable'.

    Losing the entity silently removes it from the Energy dashboard, while the
    last meter index is still perfectly valid. It must stay visible and be
    flagged `stale` instead.
    """
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data

    dernier = float(fake_measures[-1].meter_index)
    coordinator._get_last_stat = AsyncMock(
        return_value={"start": 1_760_000_000, "state": dernier, "sum": dernier}
    )
    mock_hydropolis_client.get_daily_measures = AsyncMock(
        side_effect=HydropolisApiError("server down")
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(f"sensor.hydropolis_{FAKE_CONTRAT_ID}_water_meter")
    assert state is not None
    assert state.state != "unavailable"
    assert int(float(state.state)) == int(dernier)
    assert state.attributes["stale"] is True
