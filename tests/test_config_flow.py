"""Tests for the Hydropolis Valbonne config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hydropolis_valbonne.api import (
    HydropolisApiError,
    HydropolisContract,
)
from custom_components.hydropolis_valbonne.const import (
    CONF_COMPTEUR_NUMSERIE,
    CONF_CONTRAT_ID,
    DOMAIN,
)

from .conftest import FAKE_CONTRAT_ID, FAKE_EMAIL, FAKE_PASSWORD, FAKE_SERIAL


def _make_client_mock(
    authenticate_ok: bool = True,
    contracts: list[HydropolisContract] | None = None,
) -> AsyncMock:
    client = AsyncMock()
    client.authenticate = AsyncMock(return_value=authenticate_ok)
    client.get_contracts = AsyncMock(return_value=contracts or [])
    return client


async def test_show_user_form(hass: HomeAssistant):
    """First call with no input shows the credentials form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_invalid_auth(hass: HomeAssistant):
    client = _make_client_mock(authenticate_ok=False)
    with patch(
        "custom_components.hydropolis_valbonne.config_flow.HydropolisClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "bad@example.com", CONF_PASSWORD: "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_connection_error(hass: HomeAssistant):
    client = AsyncMock()
    client.authenticate = AsyncMock(side_effect=HydropolisApiError("timeout"))
    with patch(
        "custom_components.hydropolis_valbonne.config_flow.HydropolisClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: FAKE_EMAIL, CONF_PASSWORD: FAKE_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_no_contracts(hass: HomeAssistant):
    client = _make_client_mock(authenticate_ok=True, contracts=[])
    with patch(
        "custom_components.hydropolis_valbonne.config_flow.HydropolisClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: FAKE_EMAIL, CONF_PASSWORD: FAKE_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "no_contracts"


async def test_single_contract_creates_entry(
    hass: HomeAssistant, mock_hydropolis_client
):
    """When only one contract exists, entry is created directly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: FAKE_EMAIL, CONF_PASSWORD: FAKE_PASSWORD},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Hydropolis C-9999"
    assert result["data"][CONF_CONTRAT_ID] == FAKE_CONTRAT_ID
    assert result["data"]["compteur_numserie"] == FAKE_SERIAL


async def test_multiple_contracts_shows_select(hass: HomeAssistant):
    """When multiple contracts exist, the select_contract step appears."""
    contracts = [
        HydropolisContract("1001", "C-1001", "P1", "SER1", True, "Addr A"),
        HydropolisContract("1002", "C-1002", "P2", "SER2", True, "Addr B"),
    ]
    client = _make_client_mock(authenticate_ok=True, contracts=contracts)
    with patch(
        "custom_components.hydropolis_valbonne.config_flow.HydropolisClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: FAKE_EMAIL, CONF_PASSWORD: FAKE_PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_contract"


async def test_select_contract_creates_entry(hass: HomeAssistant):
    contracts = [
        HydropolisContract("1001", "C-1001", "P1", "SER1", True, "Addr A"),
        HydropolisContract("1002", "C-1002", "P2", "SER2", True, "Addr B"),
    ]
    client = _make_client_mock(authenticate_ok=True, contracts=contracts)
    with patch(
        "custom_components.hydropolis_valbonne.config_flow.HydropolisClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: FAKE_EMAIL, CONF_PASSWORD: FAKE_PASSWORD},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CONTRAT_ID: "1002"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CONTRAT_ID] == "1002"
    assert result["data"]["compteur_numserie"] == "SER2"


async def test_duplicate_aborts(hass: HomeAssistant, mock_config_entry, mock_hydropolis_client):
    """Adding the same contract a second time aborts."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: FAKE_EMAIL, CONF_PASSWORD: FAKE_PASSWORD},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reauth / reconfigure
# ---------------------------------------------------------------------------


async def test_reauth_flow_updates_credentials(
    hass: HomeAssistant, mock_config_entry, mock_hydropolis_client
):
    """The reauth flow lets the user fix the stored credentials in place."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == FAKE_EMAIL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: FAKE_EMAIL, CONF_PASSWORD: "new-password"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-password"
    assert mock_config_entry.data[CONF_CONTRAT_ID] == FAKE_CONTRAT_ID


async def test_reauth_flow_rejects_wrong_credentials(
    hass: HomeAssistant, mock_config_entry, mock_hydropolis_client
):
    """Wrong credentials keep the form open instead of storing them."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await mock_config_entry.start_reauth_flow(hass)
    mock_hydropolis_client.authenticate = AsyncMock(return_value=False)
    mock_hydropolis_client.last_auth_error = "Le mot de passe est incorrect !"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: FAKE_EMAIL, CONF_PASSWORD: "still-wrong"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"
    assert mock_config_entry.data[CONF_PASSWORD] == FAKE_PASSWORD


async def test_reauth_flow_rejects_account_without_the_contract(
    hass: HomeAssistant, mock_config_entry, mock_hydropolis_client
):
    """Authenticating with an account that lacks this contract is refused.

    Accepting it would leave the entry pointing at a contract the account
    cannot read, and its statistics would silently stop.
    """
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await mock_config_entry.start_reauth_flow(hass)
    mock_hydropolis_client.get_contracts = AsyncMock(
        return_value=[HydropolisContract("1001", "C-1001", "P1", "SER1", True, "Addr")]
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "other@example.com", CONF_PASSWORD: "pwd"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "contract_not_found"
    assert mock_config_entry.data[CONF_USERNAME] == FAKE_EMAIL


async def test_reauth_updates_sibling_entries_of_same_account(
    hass: HomeAssistant,
    mock_config_entry,
    mock_config_entry_2,
    mock_hydropolis_client,
    fake_contract,
    fake_contract_2,
):
    """Entries sharing an account share a client, so all must be updated."""
    mock_hydropolis_client.get_contracts = AsyncMock(
        return_value=[fake_contract, fake_contract_2]
    )
    for entry in (mock_config_entry, mock_config_entry_2):
        # HA may have auto-loaded the entries already while processing tasks.
        if entry.state is not ConfigEntryState.LOADED:
            await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: FAKE_EMAIL, CONF_PASSWORD: "new-password"},
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-password"
    assert mock_config_entry_2.data[CONF_PASSWORD] == "new-password"


async def test_reconfigure_flow_updates_credentials(
    hass: HomeAssistant, mock_config_entry, mock_hydropolis_client
):
    """Credentials can also be changed proactively, without a failure."""
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "moved@example.com", CONF_PASSWORD: "new-password"},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_USERNAME] == "moved@example.com"
    assert mock_config_entry.data[CONF_PASSWORD] == "new-password"
    # The monitored contract — and therefore the statistics — are untouched.
    assert mock_config_entry.data[CONF_CONTRAT_ID] == FAKE_CONTRAT_ID
    assert mock_config_entry.data[CONF_COMPTEUR_NUMSERIE] == FAKE_SERIAL
