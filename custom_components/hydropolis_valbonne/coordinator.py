"""Data update coordinator for Hydropolis Valbonne."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    StatisticsRow,
    async_add_external_statistics,
    async_import_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .api import DailyMeasure, HydropolisApiError, HydropolisAuthError, HydropolisClient
from .const import CONF_CONTRAT_ID, DATA_REFRESH_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

FALLBACK_HISTORY_DAYS = 365 * 4

# Key in hass.data under which shared clients are stored: DOMAIN -> {username -> HydropolisClient}
SHARED_CLIENTS_KEY = f"{DOMAIN}_shared_clients"


@dataclass
class HydropolisData:
    """Latest meter reading from the API."""

    meter_total_liters: int
    last_measurement: datetime


type HydropolisConfigEntry = ConfigEntry[HydropolisCoordinator]


class HydropolisCoordinator(DataUpdateCoordinator[HydropolisData]):
    """Coordinator that fetches water meter readings from Hydropolis.

    Each refresh is incremental: it checks the last recorded statistic in the
    HA database and only fetches data from the API starting after that date.
    On the very first run (no statistics yet), it pulls all available history
    back to the contract start date.

    Statistics are imported as an external source (not "recorder") so they
    don't conflict with HA's auto-generated statistics from entity state
    changes.  The sensor entity intentionally has no state_class for the
    same reason.

    When multiple config entries share the same Hydropolis account (username),
    they reuse a single HydropolisClient so that re-authentication by one entry
    does not invalidate the Omega SSO session of the other.
    """

    _client: HydropolisClient
    config_entry: HydropolisConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: HydropolisConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DATA_REFRESH_INTERVAL,
            always_update=True,
            config_entry=config_entry,
        )
        self._contrat_id: str = config_entry.data[CONF_CONTRAT_ID]
        self._serial: str = config_entry.data["compteur_numserie"]

    @property
    def statistic_id(self) -> str:
        """Deterministic external-statistics ID for the Energy dashboard."""
        return f"{DOMAIN}:{self._contrat_id}_water_meter"

    async def _async_setup(self) -> None:
        username: str = self.config_entry.data[CONF_USERNAME]
        password: str = self.config_entry.data[CONF_PASSWORD]

        # Shared client registry: one HydropolisClient per username across all entries.
        # This ensures that two contracts from the same account never call authenticate()
        # concurrently, which would cause the Omega SSO to invalidate the first session.
        if SHARED_CLIENTS_KEY not in self.hass.data:
            self.hass.data[SHARED_CLIENTS_KEY] = {}

        shared_clients: dict[str, HydropolisClient] = self.hass.data[SHARED_CLIENTS_KEY]

        if username not in shared_clients:
            _LOGGER.debug("Creating new shared HydropolisClient for user %s", username)
            session = async_get_clientsession(self.hass)
            client = HydropolisClient(session, username, password)
            if not await client.authenticate():
                raise ConfigEntryError("Invalid credentials for Hydropolis")
            shared_clients[username] = client
        else:
            _LOGGER.debug("Reusing existing shared HydropolisClient for user %s", username)

        self._client = shared_clients[username]

    async def _async_update_data(self) -> HydropolisData | None:
        """Incremental fetch: get data from last known stat to today.

        First run pulls full history; subsequent runs only the gap.
        Also imports all fetched data into HA long-term statistics so
        the Energy dashboard has complete history.

        Returns None (instead of raising) when the API has no data yet,
        so the integration loads normally and the sensor can fall back
        to its restored state.
        """
        today = dt_util.now().date()

        last_stat = await self._get_last_stat()
        if last_stat is not None:
            last_recorded = datetime.fromtimestamp(last_stat["start"]).date()
            start = last_recorded + timedelta(days=1)
            _LOGGER.debug(
                "Incremental fetch from %s (last stat: %s)", start, last_recorded
            )
        else:
            start = self._client.data_available_since_for(self._contrat_id)
            if start is None:
                start = today - timedelta(days=FALLBACK_HISTORY_DAYS)
            _LOGGER.info("First fetch — pulling history from %s", start)

        try:
            measures = await self._client.get_daily_measures(
                self._contrat_id, self._serial, start, today
            )
        except (HydropolisApiError, HydropolisAuthError) as err:
            raise UpdateFailed(f"Error fetching Hydropolis data: {err}") from err

        if not measures:
            if self.data is not None:
                _LOGGER.debug("No new measures, keeping previous reading")
                return self.data
            if last_stat is not None:
                _LOGGER.info(
                    "No new measures from API; restoring from last recorded statistic"
                )
                return HydropolisData(
                    meter_total_liters=int(last_stat["sum"]),
                    last_measurement=datetime.fromtimestamp(last_stat["start"]),
                )
            _LOGGER.info("No measures available from Hydropolis API yet")
            return None

        self._import_statistics(measures)

        latest = measures[-1]
        _LOGGER.debug(
            "Hydropolis meter: %d L at %s (%d measures fetched)",
            latest.meter_index,
            latest.timestamp.isoformat(),
            len(measures),
        )

        return HydropolisData(
            meter_total_liters=latest.meter_index,
            last_measurement=latest.timestamp,
        )

    def _import_statistics(self, measures: list[DailyMeasure]) -> None:
        """Push fetched daily measures into HA long-term statistics.

        Uses an external source (DOMAIN, not "recorder") so the entries
        are independent of any auto-generated statistics from entity state
        changes.  The Energy dashboard can pick them up by statistic_id.
        """
        statistics: list[StatisticData] = []
        for measure in measures:
            if measure.consumption_liters < 0:
                continue
            statistics.append(
                StatisticData(
                    start=dt_util.start_of_local_day(measure.date),
                    state=float(measure.meter_index),
                    sum=float(measure.meter_index),
                )
            )

        if not statistics:
            return

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Hydropolis {self._contrat_id} Water",
            source=DOMAIN,
            statistic_id=self.statistic_id,
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.LITERS,
        )

        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug(
            "Imported %d statistics entries to %s", len(statistics), self.statistic_id
        )

    async def _get_last_stat(self) -> StatisticsRow | None:
        """Find the most recent recorded statistic."""
        from homeassistant.components.recorder import get_instance

        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, self.statistic_id, True, {"sum", "state"}
        )
        return last[self.statistic_id][0] if last else None
