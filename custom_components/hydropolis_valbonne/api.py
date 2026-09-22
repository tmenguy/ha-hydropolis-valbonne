"""API client for Hydropolis Valbonne water utility."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime
import json
import logging

import aiohttp

from .const import (
    OMEGA_API_ID,
    OMEGA_API_URL,
    OMEGA_SSO_URL,
    THREINT_API_ID,
    THREINT_API_URL,
)

_LOGGER = logging.getLogger(__name__)

JSONAPI_CONTENT_TYPE = "application/vnd.api+json"

# Statuses for which the SSO is telling us the login/password pair is wrong.
# The JVS Omega SSO answers 409 with a JSON:API error body — 10300002 for a
# wrong password, 10300004 for an unknown login — and not the more usual 401.
# Anything else (5xx, 429, ...) is transient and must not be reported as
# "invalid credentials", otherwise the integration fails permanently on a
# simple server-side hiccup.
CREDENTIAL_REJECTED_STATUSES = frozenset({400, 401, 403, 409, 422})


class HydropolisAuthError(Exception):
    """Raised when authentication fails."""


class HydropolisApiError(Exception):
    """Raised when an API call fails."""


@dataclass
class HydropolisContract:
    """A water contract returned by the Omega API."""

    contrat_id: str
    numcontrat: str
    pconso_id: str
    compteur_numserie: str
    actif: bool
    address: str | None = None


@dataclass
class DailyMeasure:
    """A single daily consumption measure from the 3Int API."""

    date: date
    timestamp: datetime
    consumption_liters: int
    meter_index: int


class HydropolisClient:
    """Async client for the Hydropolis / JVS Omega / 3Int APIs.

    A single instance of this client is shared across all coordinators that
    belong to the same Hydropolis account (same username/password). This avoids
    the problem where two coordinators calling authenticate() independently
    would cause the Omega SSO server to invalidate each other's session.

    The Omega SSO token (_omega_token) is therefore shared.
    The 3Int tokens are per-contract and stored in _3int_tokens[contrat_id],
    so each contract maintains its own independent 3Int session.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password

        # Human-readable reason of the last credential rejection, straight from
        # the SSO ("Le mot de passe est incorrect !", ...).
        self.last_auth_error: str | None = None

        self._omega_token: str | None = None
        self._omega_sso_id: str | None = None
        self._omega_app_id: str | None = None

        # Per-contract 3Int tokens and data_available_since dates
        self._3int_tokens: dict[str, str] = {}
        self._data_available_since: dict[str, date] = {}

    def credentials_match(self, username: str, password: str) -> bool:
        """Whether this client still holds the given credentials.

        Used by the coordinator to detect that a config entry was re-authed
        with a new password and that the cached shared client is stale.
        """
        return self._username == username and self._password == password

    async def authenticate(self) -> bool:
        """Authenticate against the Omega SSO.

        Returns True on success and False only when the server explicitly
        rejected the credentials.  Any other failure (server error, rate
        limiting, maintenance) raises HydropolisApiError so callers can treat
        it as a transient condition and retry instead of declaring the
        credentials invalid.
        """
        self._omega_token = None
        try:
            resp = await self._session.post(
                f"{OMEGA_SSO_URL}/v1/sso/signin",
                json={"login": self._username, "password": self._password, "remember": False},
                headers={
                    "Content-Type": JSONAPI_CONTENT_TYPE,
                    "Accept": JSONAPI_CONTENT_TYPE,
                },
            )
        except aiohttp.ClientError as err:
            raise HydropolisApiError(f"Connection error during login: {err}") from err

        if resp.status in CREDENTIAL_REJECTED_STATUSES:
            self.last_auth_error = await self._error_detail(resp)
            _LOGGER.warning(
                "Hydropolis rejected the credentials of %s (HTTP %s): %s",
                self._username,
                resp.status,
                self.last_auth_error or "no detail given",
            )
            return False

        if resp.status != 201:
            raise HydropolisApiError(f"SSO login returned {resp.status}")

        self.last_auth_error = None

        self._omega_token = resp.headers.get("authorization")
        self._omega_sso_id = resp.headers.get("ssoid")
        self._omega_app_id = resp.headers.get("appid")

        if not self._omega_token:
            _LOGGER.error("SSO login succeeded but no authorization token in response")
            return False

        # Invalidate all 3Int tokens when Omega token is refreshed —
        # they are bound to the Omega session.
        self._3int_tokens.clear()

        return True

    @staticmethod
    async def _error_detail(resp: aiohttp.ClientResponse) -> str | None:
        """Extract the message of a JSON:API error response, if any."""
        try:
            body = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            return None

        if not isinstance(body, dict):
            return None
        errors = body.get("errors")
        if not isinstance(errors, list) or not errors:
            return None
        first = errors[0]
        if not isinstance(first, dict):
            return None
        detail = first.get("detail") or first.get("title")
        return str(detail) if detail else None

    def _omega_headers(self) -> dict[str, str]:
        """Build headers for Omega API calls."""
        headers: dict[str, str] = {
            "Content-Type": JSONAPI_CONTENT_TYPE,
            "Accept": JSONAPI_CONTENT_TYPE,
            "ApiId": OMEGA_API_ID,
        }
        if self._omega_token:
            headers["Authorization"] = f"Bearer {self._omega_token}"
        if self._omega_sso_id:
            headers["SsoId"] = self._omega_sso_id
        if self._omega_app_id:
            headers["AppId"] = self._omega_app_id
        return headers

    async def get_contracts(self) -> list[HydropolisContract]:
        """Fetch the list of water contracts for the authenticated user."""
        if not self._omega_token:
            raise HydropolisAuthError("Not authenticated")

        try:
            resp = await self._session.get(
                f"{OMEGA_API_URL}/v1/iclient/contrat?len=0",
                headers=self._omega_headers(),
            )
        except aiohttp.ClientError as err:
            raise HydropolisApiError(f"Error fetching contracts: {err}") from err

        if resp.status != 200:
            raise HydropolisApiError(f"Contracts endpoint returned {resp.status}")

        data = await resp.json(content_type=None)
        contracts: list[HydropolisContract] = []

        # Build lookup tables from JSON:API included resources.
        # We keep both attributes AND relationships for each included item
        # because the link contrat→compteur goes through IClient_Pconso.
        included_by_type: dict[str, dict[str, dict]] = {}
        for item in data.get("included", []):
            t = item.get("type", "")
            iid = item.get("id", "")
            included_by_type.setdefault(t, {})[iid] = {
                "attributes": item.get("attributes", {}),
                "relationships": item.get("relationships", {}),
            }

        for contrat in data.get("data", []):
            attrs = contrat.get("attributes", {})
            relationships = contrat.get("relationships", {})

            # Navigate: contrat.attributes.pconso_id
            #        → IClient_Pconso[pconso_id].relationships.compteur.id
            #        → IClient_Compteur[compteur_id].attributes.numserie
            pconso_id = attrs.get("pconso_id", "")
            pconso = included_by_type.get("IClient_Pconso", {}).get(pconso_id, {})
            compteur_ref = pconso.get("relationships", {}).get("compteur", {}).get("data", {})
            if isinstance(compteur_ref, list):
                compteur_ref = compteur_ref[0] if compteur_ref else {}
            compteur_id = compteur_ref.get("id", "")
            compteur_attrs = included_by_type.get("IClient_Compteur", {}).get(compteur_id, {}).get("attributes", {})
            numserie = compteur_attrs.get("numserie", "")

            # Address: IClient_Pdessadr via pconso
            pdessadr_ref = pconso.get("relationships", {}).get("pdessadr", {}).get("data", {})
            if isinstance(pdessadr_ref, list):
                pdessadr_ref = pdessadr_ref[0] if pdessadr_ref else {}
            pdessadr_id = pdessadr_ref.get("id", "")
            pdessadr_attrs = included_by_type.get("IClient_Pdessadr", {}).get(pdessadr_id, {}).get("attributes", {})
            address = pdessadr_attrs.get("libvoie", "") or pdessadr_attrs.get("cpltadr", "") or pconso.get("attributes", {}).get("cpltadr", "")

            contracts.append(
                HydropolisContract(
                    contrat_id=attrs.get("contrat_id", ""),
                    numcontrat=attrs.get("numcontrat", ""),
                    pconso_id=attrs.get("pconso_id", ""),
                    compteur_numserie=numserie,
                    actif=attrs.get("actif") == "1",
                    address=address or None,
                )
            )

        return contracts

    async def _authenticate_3int(self, contrat_id: str, serial: str) -> None:
        """Exchange the Omega token for a 3Int API token (per contract)."""
        try:
            resp = await self._session.post(
                f"{THREINT_API_URL}/authentication_token",
                json={
                    "jvstoken": self._omega_token,
                    "ApiId": THREINT_API_ID,
                    "serial": serial,
                    "contrat_id": contrat_id,
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        except aiohttp.ClientError as err:
            raise HydropolisApiError(f"3Int auth error: {err}") from err

        if resp.status in (401, 403):
            raise HydropolisAuthError(
                f"3Int auth rejected the Omega session (status {resp.status})"
            )

        if resp.status != 200:
            raise HydropolisApiError(f"3Int auth returned {resp.status}")

        body = await resp.json()
        token = body.get("token")
        if not token:
            raise HydropolisApiError("3Int auth succeeded but no token returned")

        self._3int_tokens[contrat_id] = token

        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            datedeb_str = claims.get("datedeb", "")
            if datedeb_str:
                self._data_available_since[contrat_id] = datetime.fromisoformat(
                    datedeb_str.strip()
                ).date()
        except (IndexError, ValueError, TypeError):
            _LOGGER.debug("Could not parse datedeb from 3Int JWT for contrat %s", contrat_id)

    async def _ensure_3int_token(self, contrat_id: str, serial: str) -> None:
        """Obtain a 3Int token, refreshing the Omega session if it was refused.

        A 401 from the token exchange means the Omega session behind it went
        stale, not that the account credentials are wrong — so log in again and
        retry once before giving up.
        """
        try:
            await self._authenticate_3int(contrat_id, serial)
        except HydropolisAuthError:
            _LOGGER.debug(
                "3Int refused the Omega session for contrat %s, re-authenticating",
                contrat_id,
            )
            if not await self.authenticate():
                raise HydropolisAuthError(
                    self.last_auth_error or "Failed to re-authenticate with Omega SSO"
                ) from None
            await self._authenticate_3int(contrat_id, serial)

    async def get_daily_measures(
        self,
        contrat_id: str,
        serial: str,
        start: date,
        end: date,
    ) -> list[DailyMeasure]:
        """Fetch daily consumption measures for a date range.

        The 3Int API paginates at ~365 items per page. This method iterates
        through all pages so callers always receive the complete result set.
        Handles full re-authentication if needed (Omega SSO + 3Int token).
        """
        if not self._omega_token:
            if not await self.authenticate():
                raise HydropolisAuthError(
                    self.last_auth_error or "Failed to authenticate with Omega SSO"
                )

        if contrat_id not in self._3int_tokens:
            await self._ensure_3int_token(contrat_id, serial)

        start_str = start.strftime("%Y-%m-%d") + "T00:00:00"
        end_str = end.strftime("%Y-%m-%d") + "T23:59:59"

        base_url = (
            f"{THREINT_API_URL}/measures"
            f"?dateStatement[after]={start_str}"
            f"&dateStatement[before]={end_str}"
            f"&order[dateStatement]=asc"
        )

        measures: list[DailyMeasure] = []
        page = 1

        while True:
            url = f"{base_url}&page={page}"
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._3int_tokens[contrat_id]}",
            }

            try:
                resp = await self._session.get(url, headers=headers)
            except aiohttp.ClientError as err:
                raise HydropolisApiError(f"Measures fetch error: {err}") from err

            if resp.status == 401:
                _LOGGER.debug(
                    "3Int token expired for contrat %s, re-authenticating", contrat_id
                )
                # Only invalidate the 3Int token for this specific contract.
                # Re-authenticate Omega only if truly needed (token missing/expired).
                self._3int_tokens.pop(contrat_id, None)
                if not self._omega_token:
                    if not await self.authenticate():
                        raise HydropolisAuthError(
                            self.last_auth_error
                            or "Failed to re-authenticate with Omega SSO"
                        )
                await self._ensure_3int_token(contrat_id, serial)
                # Retry once with the fresh token
                headers["Authorization"] = f"Bearer {self._3int_tokens[contrat_id]}"
                try:
                    resp = await self._session.get(url, headers=headers)
                except aiohttp.ClientError as err:
                    raise HydropolisApiError(f"Measures fetch error after retry: {err}") from err
                if resp.status != 200:
                    raise HydropolisApiError(
                        f"Measures endpoint returned {resp.status} after re-auth"
                    )

            elif resp.status != 200:
                raise HydropolisApiError(f"Measures endpoint returned {resp.status}")

            raw = await resp.json()
            if not raw:
                break

            for item in raw:
                try:
                    dt_str = item["dateStatement"]
                    dt = datetime.fromisoformat(dt_str)
                    consumption = int(item.get("consumption", 0))
                    last_index = item.get("lastIndex", {})
                    meter_value = int(last_index.get("Value", 0))

                    measures.append(
                        DailyMeasure(
                            date=dt.date(),
                            timestamp=dt,
                            consumption_liters=consumption,
                            meter_index=meter_value,
                        )
                    )
                except (KeyError, ValueError, TypeError) as err:
                    _LOGGER.debug("Skipping malformed measure: %s", err)
                    continue

            _LOGGER.debug("Page %d: %d items fetched", page, len(raw))
            page += 1

        return measures

    def data_available_since_for(self, contrat_id: str) -> date | None:
        """Earliest date with data for a given contract, extracted from the 3Int JWT."""
        return self._data_available_since.get(contrat_id)

    def invalidate_tokens(self) -> None:
        """Clear cached tokens, forcing re-authentication on next call."""
        self._omega_token = None
        self._3int_tokens.clear()
