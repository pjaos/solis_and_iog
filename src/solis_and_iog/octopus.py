from datetime import datetime, timezone
import requests

# ---------------------------------------------------------------------------
# OctopusClient
# ---------------------------------------------------------------------------
class OctopusClient:
    """
    Wraps the Octopus Energy GraphQL API.
    Responsible for fetching planned dispatches and identifying extra
    (outside standard off-peak) slots for Intelligent Go customers.
    """

    GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"

    PLANNED_DISPATCHES_QUERY = """
    query PlannedDispatches($deviceId: String!) {
      flexPlannedDispatches(deviceId: $deviceId) {
        start
        end
      }
    }
    """

    OBTAIN_TOKEN_MUTATION = """
    mutation ObtainKrakenToken($apiKey: String!) {
      obtainKrakenToken(input: { APIKey: $apiKey }) {
        token
      }
    }
    """

    def __init__(self,
                 api_key: str,
                 account_number: str,
                 offpeak_start: tuple[int, int] = (23, 30),
                 offpeak_end:   tuple[int, int] = (5,  30),
                 uio = None):
        self.api_key        = api_key
        self.account_number = account_number
        self.offpeak_start  = offpeak_start
        self.offpeak_end    = offpeak_end
        self._uio            = uio
        self._token:        str | None = None
        self._device_id:    str | None = None

    def _debug(self, msg):
        if self._uio:
            self._uio.debug(f"Octopus API: {msg}")

    def _warn(self, msg):
        if self._uio:
            self._uio.warn(f"Octopus API: {msg}")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def find_active_extra_dispatch(self) -> dict | None:
        """
        Return the first dispatch that is currently active and falls outside
        the standard off-peak window, or None.
        """
        now = datetime.now(timezone.utc)
        for d in self._get_planned_dispatches():
            try:
                start = self._parse_dt(d["start"])
                end   = self._parse_dt(d["end"])
            except (KeyError, ValueError) as exc:
                self._debug(f"Skipping malformed dispatch: {exc}")
                continue
            if start <= now <= end and self._is_outside_offpeak(start, end):
                return {"start": start, "end": end, "raw": d}
        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_token(self) -> str | None:
        """Obtain (or reuse a cached) Kraken API token."""
        if self._token:
            return self._token
        try:
            resp = requests.post(
                self.GRAPHQL_URL,
                json={
                    "query":     self.OBTAIN_TOKEN_MUTATION,
                    "variables": {"apiKey": self.api_key},
                },
                timeout=15,
            )
            self._debug(f"Token response status: {resp.status_code}")
            self._debug(f"Token response body: {resp.text}")
            resp.raise_for_status()
            token = resp.json().get("data", {}).get("obtainKrakenToken", {}).get("token")
            if not token:
                self._warn("Octopus token request returned no token.")
                return None
            self._token = token
            self._debug("Obtained Octopus Kraken token.")
            return self._token
        except Exception as exc:
            self._warn(f"Failed to obtain Octopus token: {exc}")
            return None

    def _get_device_id(self) -> str | None:
        """Look up the EV device ID from the Octopus account."""
        if self._device_id:
            return self._device_id
        token = self._get_token()
        if not token:
            return None
        try:
            resp = requests.post(
                self.GRAPHQL_URL,
                json={
                    "query": """
                    query GetDevices($accountNumber: String!) {
                      devices(accountNumber: $accountNumber) {
                        id
                        deviceType
                      }
                    }
                    """,
                    "variables": {"accountNumber": self.account_number},
                },
                headers={"Authorization": token},
                timeout=15,
            )
            resp.raise_for_status()
            devices = resp.json().get("data", {}).get("devices", []) or []
            for device in devices:
                if device.get("deviceType") == "ELECTRIC_VEHICLES":
                    self._device_id = device["id"]
                    self._debug(f"Found EV device ID: {self._device_id}")
                    return self._device_id
            self._warn("No ELECTRIC_VEHICLES device found on Octopus account.")
            return None
        except Exception as exc:
            self._warn(f"Failed to look up Octopus device ID: {exc}")
            return None

    def _is_token_expired(self, data: dict) -> bool:
        """Return True if the response contains a JWT expiry error."""
        errors = data.get("errors", [])
        return any("KT-CT-1124" in e.get("extensions", {}).get("errorCode", "")
                   for e in errors)

    def _get_planned_dispatches(self) -> list[dict]:
        """Return list of planned dispatch dicts from the Octopus API."""
        token = self._get_token()
        if not token:
            self._warn("Skipping dispatch fetch — no valid Octopus token.")
            return []
        device_id = self._get_device_id()
        if not device_id:
            self._warn("Skipping dispatch fetch — no EV device ID found.")
            return []
        try:
            resp = requests.post(
                self.GRAPHQL_URL,
                json={
                    "query":     self.PLANNED_DISPATCHES_QUERY,
                    "variables": {"deviceId": device_id},
                },
                headers={"Authorization": token},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            # Token may have expired — Octopus returns 200 with error body
            if resp.status_code == 401 or self._is_token_expired(data):
                self._debug("Octopus token expired, refreshing.")
                self._token = None
                token = self._get_token()
                if not token:
                    return []
                resp = requests.post(
                    self.GRAPHQL_URL,
                    json={
                        "query":     self.PLANNED_DISPATCHES_QUERY,
                        "variables": {"deviceId": device_id},
                    },
                    headers={"Authorization": token},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()

            self._debug(f"Dispatches response status: {resp.status_code}")
            self._debug(f"Dispatches response body: {resp.text}")
            dispatches = data.get("data", {}).get("flexPlannedDispatches", []) or []
            now = datetime.now()
            now_str = now.astimezone().strftime("%H:%M:%S %d:%m:%Y")
            self._debug(f"Octopus returned {len(dispatches)} planned dispatch(es): at {now_str} (local time).")
            return dispatches
        except Exception as exc:
            self._warn(f"Failed to fetch Octopus dispatches: {exc}")
            self._token = None  # clear token on error so next poll retries
            return []

    @staticmethod
    def _parse_dt(dt_str: str) -> datetime:
        """Parse an ISO-8601 datetime string to an aware datetime (UTC)."""
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _is_outside_offpeak(self, start: datetime, end: datetime) -> bool:
        """
        Return True if the slot extends outside the standard off-peak window,
        meaning Octopus has added an extra cheap slot specifically for the EV.

        Checks that the slot is entirely contained within 23:30-05:30, not just
        that both endpoints appear to fall within the window. For example, a slot
        running 23:00-06:00 has endpoints that look in-range but actually extends
        outside the window on both sides.
        """
        local_start = start.astimezone()
        local_end   = end.astimezone()

        def to_minutes(t: datetime) -> int:
            """Convert a time to minutes since midnight."""
            return t.hour * 60 + t.minute

        def slot_contained_in_offpeak(s: datetime, e: datetime) -> bool:
            """
            Return True only if the entire slot falls within the off-peak window.
            Checks that the slot doesn't start before off-peak begins AND doesn't
            end after off-peak ends.
            """
            s_mins   = to_minutes(s)
            e_mins   = to_minutes(e)
            op_start = self.offpeak_start[0] * 60 + self.offpeak_start[1]  # e.g. 23:30 = 1410
            op_end   = self.offpeak_end[0]   * 60 + self.offpeak_end[1]    # e.g. 05:30 =  330

            # Start must be at or after 23:30, or past midnight but before/at 05:30
            start_ok = s_mins >= op_start or s_mins <= op_end
            # End must be at or before 05:30
            end_ok   = e_mins <= op_end
            return start_ok and end_ok

        # If the slot is entirely within the standard overnight window,
        # the battery's fixed schedule already handles it — skip it.
        return not slot_contained_in_offpeak(local_start, local_end)