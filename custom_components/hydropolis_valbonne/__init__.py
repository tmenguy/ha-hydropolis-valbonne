"""The Hydropolis Valbonne integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import (
    SHARED_CLIENTS_KEY,
    HydropolisConfigEntry,
    HydropolisCoordinator,
)

PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: HydropolisConfigEntry) -> bool:
    """Set up Hydropolis Valbonne from a config entry."""
    coordinator = HydropolisCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: HydropolisConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: HydropolisConfigEntry) -> None:
    """Drop the shared HydropolisClient when the last entry for a user is removed."""
    username = entry.data.get(CONF_USERNAME)
    if not username:
        return

    other_entries_for_user = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
        and other.data.get(CONF_USERNAME) == username
    ]
    if other_entries_for_user:
        return

    shared_clients = hass.data.get(SHARED_CLIENTS_KEY, {})
    if shared_clients.pop(username, None) is not None:
        _LOGGER.debug("Removed shared HydropolisClient for user %s", username)
