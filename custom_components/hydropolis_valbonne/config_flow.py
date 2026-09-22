"""Config flow for Hydropolis Valbonne integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HydropolisApiError, HydropolisAuthError, HydropolisClient, HydropolisContract
from .const import CONF_COMPTEUR_NUMSERIE, CONF_CONTRAT_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class HydropolisConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hydropolis Valbonne."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._username: str = ""
        self._password: str = ""
        self._contracts: list[HydropolisContract] = []

    # ------------------------------------------------------------------
    # Initial setup
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the credentials step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                self._contracts = await self._async_fetch_contracts(
                    self._username, self._password
                )
            except AbortFlow:
                raise
            except InvalidAuth as err:
                _LOGGER.debug("Hydropolis rejected the credentials: %s", err)
                errors["base"] = "invalid_auth"
            except NoContracts:
                errors["base"] = "no_contracts"
            except HydropolisApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during authentication")
                errors["base"] = "unknown"
            else:
                if len(self._contracts) == 1:
                    return await self._create_entry(self._contracts[0])
                return await self.async_step_select_contract()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_select_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick a contract when multiple are available."""
        if user_input is not None:
            selected_id = user_input[CONF_CONTRAT_ID]
            contract = next(
                (c for c in self._contracts if c.contrat_id == selected_id), None
            )
            if contract is None:
                return self.async_abort(reason="unknown")
            return await self._create_entry(contract)

        options = {
            c.contrat_id: f"{c.numcontrat} — {c.address or c.contrat_id}"
            for c in self._contracts
        }

        return self.async_show_form(
            step_id="select_contract",
            data_schema=vol.Schema(
                {vol.Required(CONF_CONTRAT_ID): vol.In(options)}
            ),
        )

    async def _create_entry(self, contract: HydropolisContract) -> ConfigFlowResult:
        """Create config entry for the selected contract."""
        await self.async_set_unique_id(contract.contrat_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Hydropolis {contract.numcontrat}",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_CONTRAT_ID: contract.contrat_id,
                CONF_COMPTEUR_NUMSERIE: contract.compteur_numserie,
            },
        )

    # ------------------------------------------------------------------
    # Reauth — triggered by ConfigEntryAuthFailed
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication after the credentials were rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for fresh credentials and update the existing entry."""
        return await self._async_credentials_step(
            "reauth_confirm", self._get_reauth_entry(), user_input
        )

    # ------------------------------------------------------------------
    # Reconfigure — available at any time from the entry menu
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user review and change the stored credentials."""
        return await self._async_credentials_step(
            "reconfigure", self._get_reconfigure_entry(), user_input
        )

    # ------------------------------------------------------------------
    # Shared credentials handling
    # ------------------------------------------------------------------

    async def _async_credentials_step(
        self,
        step_id: str,
        entry: ConfigEntry,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Credentials form shared by the reauth and reconfigure steps.

        The contract itself is never changed here: its ID is the entry's
        unique ID and the key of the imported long-term statistics, so
        switching contract would orphan the recorded history.  Only the
        account credentials (and the meter serial, which can change when the
        meter is replaced) are updated.
        """
        errors: dict[str, str] = {}
        current_username: str = entry.data[CONF_USERNAME]
        username = current_username

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            try:
                contracts = await self._async_fetch_contracts(username, password)
            except AbortFlow:
                raise
            except InvalidAuth as err:
                _LOGGER.debug("Hydropolis rejected the credentials: %s", err)
                errors["base"] = "invalid_auth"
            except NoContracts:
                errors["base"] = "no_contracts"
            except HydropolisApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during authentication")
                errors["base"] = "unknown"
            else:
                contrat_id = entry.data[CONF_CONTRAT_ID]
                contract = next(
                    (c for c in contracts if c.contrat_id == contrat_id), None
                )
                if contract is None:
                    # The account is valid but does not own this contract:
                    # the user most likely typed another account's email.
                    errors["base"] = "contract_not_found"
                else:
                    return await self._async_apply_credentials(
                        entry, current_username, username, password, contract
                    )

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=username): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={
                "username": current_username,
                "contract": entry.title,
            },
            errors=errors,
        )

    async def _async_apply_credentials(
        self,
        entry: ConfigEntry,
        old_username: str,
        username: str,
        password: str,
        contract: HydropolisContract,
    ) -> ConfigFlowResult:
        """Store the new credentials on this entry and its siblings.

        All entries sharing the same account also share a single
        HydropolisClient, so they must all be updated and reloaded together —
        otherwise the untouched entries keep authenticating with the old
        password.
        """
        for other in self.hass.config_entries.async_entries(DOMAIN):
            if other.entry_id == entry.entry_id:
                continue
            if other.data.get(CONF_USERNAME) != old_username:
                continue
            self.hass.config_entries.async_update_entry(
                other,
                data={
                    **other.data,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                },
            )
            self.hass.config_entries.async_schedule_reload(other.entry_id)

        updates: dict[str, Any] = {
            CONF_USERNAME: username,
            CONF_PASSWORD: password,
        }
        if contract.compteur_numserie:
            updates[CONF_COMPTEUR_NUMSERIE] = contract.compteur_numserie

        return self.async_update_reload_and_abort(entry, data_updates=updates)

    async def _async_fetch_contracts(
        self, username: str, password: str
    ) -> list[HydropolisContract]:
        """Authenticate and return the account's contracts.

        Raises InvalidAuth when the credentials are rejected, NoContracts when
        the account has none, and HydropolisApiError for transient failures.
        """
        session = async_get_clientsession(self.hass)
        client = HydropolisClient(session, username, password)

        try:
            authenticated = await client.authenticate()
        except HydropolisAuthError as err:
            raise InvalidAuth(str(err)) from err

        if not authenticated:
            raise InvalidAuth(client.last_auth_error or "credentials rejected")

        contracts = await client.get_contracts()
        if not contracts:
            raise NoContracts
        return contracts


class InvalidAuth(Exception):
    """Credentials were rejected by the Hydropolis SSO."""


class NoContracts(Exception):
    """The account has no water contract."""
