"""
oct_int_go_solis_integration.py
================================
Monitors Octopus Energy Intelligent Go for extra (outside 23:30-05:30) dispatch
slots and mirrors them as charge-time windows on a Solis AC-coupled inverter via
the SolisCloud Control API.

How it works:
    1. Every POLL_INTERVAL seconds the Octopus GraphQL API is queried for
       plannedDispatches.
    2. Any dispatch slot whose start OR end falls outside the standard off-peak
       window (23:30-05:30) is considered an "intelligent" extra slot.
    3. If we are currently inside such a slot and no Solis schedule is active,
       one is created (charge time slot 3, which we reserve for this purpose).
    4. When the slot ends the schedule is cleared.

SolisCloud API notes:
    - You need a SolisCloud API Key ID and Secret. Request access at:
      https://solis-service.solisinverters.com/en/support/solutions/articles/44002212561
    - The control API uses HMAC-SHA1 signed requests.
    - Solis inverters have 3 charge time slots. This script uses slot 3 by
      default (TIME_SLOT = 3) so as not to conflict with your fixed overnight
      schedule in slots 1 & 2.
    - "Allow Grid Charging" must be enabled on the inverter (via SolisCloud app
      or the inverter display).
"""

import argparse
import hashlib
import hmac
import json
import os
import time
from base64 import b64encode
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from p3lib.uio import UIO
from p3lib.helper import logTraceBack
from p3lib.boot_manager import BootManager
from p3lib.helper import get_program_version

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
uio = UIO()


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

    def __init__(self, api_key: str, account_number: str,
                 offpeak_start: tuple[int, int] = (23, 30),
                 offpeak_end:   tuple[int, int] = (5,  30)):
        self.api_key        = api_key
        self.account_number = account_number
        self.offpeak_start  = offpeak_start
        self.offpeak_end    = offpeak_end
        self._token:        str | None = None
        self._device_id:    str | None = None

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
                uio.debug(f"Skipping malformed dispatch: {exc}")
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
            uio.debug(f"Token response status: {resp.status_code}")
            uio.debug(f"Token response body: {resp.text}")
            resp.raise_for_status()
            token = resp.json().get("data", {}).get("obtainKrakenToken", {}).get("token")
            if not token:
                uio.warn("Octopus token request returned no token.")
                return None
            self._token = token
            uio.debug("Obtained Octopus Kraken token.")
            return self._token
        except Exception as exc:
            uio.warn(f"Failed to obtain Octopus token: {exc}")
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
                    uio.debug(f"Found EV device ID: {self._device_id}")
                    return self._device_id
            uio.warn("No ELECTRIC_VEHICLES device found on Octopus account.")
            return None
        except Exception as exc:
            uio.warn(f"Failed to look up Octopus device ID: {exc}")
            return None

    def _get_planned_dispatches(self) -> list[dict]:
        """Return list of planned dispatch dicts from the Octopus API."""
        token = self._get_token()
        if not token:
            uio.warn("Skipping dispatch fetch — no valid Octopus token.")
            return []
        device_id = self._get_device_id()
        if not device_id:
            uio.warn("Skipping dispatch fetch — no EV device ID found.")
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
            if resp.status_code == 401:
                # Token may have expired — clear cache and retry once
                uio.debug("Octopus token expired, refreshing.")
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
            uio.debug(f"Dispatches response status: {resp.status_code}")
            uio.debug(f"Dispatches response body: {resp.text}")
            resp.raise_for_status()
            data = resp.json()
            dispatches = data.get("data", {}).get("flexPlannedDispatches", []) or []
            uio.debug(f"Octopus returned {len(dispatches)} planned dispatch(es)")
            return dispatches
        except Exception as exc:
            uio.warn(f"Failed to fetch Octopus dispatches: {exc}")
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


# ---------------------------------------------------------------------------
# SolisClient
# ---------------------------------------------------------------------------
class SolisClient:
    """
    Wraps the SolisCloud Control API v2.
    Responsible for reading and writing charge time slots on the inverter.
    """

    CONTROL_PATH = "/v2/api/control"
    READ_PATH    = "/v2/api/atRead"
    SCHEDULE_CID = "103"

    def __init__(self, key_id: str, key_secret: str, inverter_sn: str,
                 api_url: str = "https://www.soliscloud.com:13333",
                 time_slot: int = 3):
        self.key_id      = key_id
        self.key_secret  = key_secret
        self.inverter_sn = inverter_sn
        self.api_url     = api_url.rstrip("/")
        self.time_slot   = time_slot

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_charge_slot(self, start: datetime, end: datetime) -> bool:
        """
        Write a charge time window into the inverter's reserved time slot.
        Reads the current schedule first so other slots are not disturbed.
        """
        schedule = self._read_schedule()
        if schedule is None:
            uio.error("Cannot read current Solis schedule — aborting slot write.")
            return False

        start_s = self.fmt_time(start)
        end_s   = self.fmt_time(end)

        idx = self.time_slot - 1
        schedule["charge"][idx] = {"start": start_s, "end": end_s, "enable": 1}

        uio.info(f"Setting Solis charge slot {self.time_slot}: {start_s} -> {end_s}")
        result = self._post(self.CONTROL_PATH, {
            "inverterSn": self.inverter_sn,
            "cid":        self.SCHEDULE_CID,
            "value":      self._build_value_string(schedule),
        })
        success = result.get("code") == "0" or result.get("success") is True
        if success:
            uio.info(f"Solis charge slot {self.time_slot} set successfully.")
        else:
            uio.error(f"Solis API returned unexpected response: {result}")
        return success

    def clear_charge_slot(self) -> bool:
        """Zero out the charge time slot used by this script."""
        schedule = self._read_schedule()
        if schedule is None:
            uio.error("Cannot read current Solis schedule — aborting slot clear.")
            return False

        idx = self.time_slot - 1
        schedule["charge"][idx] = {"start": "00:00", "end": "00:00", "enable": 0}

        uio.info(f"Clearing Solis charge slot {self.time_slot}.")
        result = self._post(self.CONTROL_PATH, {
            "inverterSn": self.inverter_sn,
            "cid":        self.SCHEDULE_CID,
            "value":      self._build_value_string(schedule),
        })
        success = result.get("code") == "0" or result.get("success") is True
        if success:
            uio.info(f"Solis charge slot {self.time_slot} cleared.")
        else:
            uio.error(f"Solis clear returned unexpected response: {result}")
        return success

    # ------------------------------------------------------------------
    # Private helpers — API communication
    # ------------------------------------------------------------------

    def _post(self, path: str, body: dict) -> dict:
        """POST to the SolisCloud Control API with correct authentication headers."""
        payload      = json.dumps(body)
        content_md5  = b64encode(hashlib.md5(payload.encode()).digest()).decode()
        content_type = "application/json"
        date         = formatdate(usegmt=True)
        signature    = self._sign(content_md5, content_type, date, path)

        headers = {
            "Content-Type":  content_type,
            "Content-MD5":   content_md5,
            "Date":          date,
            "Authorization": f"API {self.key_id}:{signature}",
        }
        try:
            resp = requests.post(
                self.api_url + path,
                headers=headers,
                data=payload,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            uio.error(f"SolisCloud API error ({path}): {exc}")
            return {}

    def _sign(self, content_md5: str, content_type: str,
              date: str, path: str) -> str:
        """Produce the HMAC-SHA1 signature SolisCloud expects."""
        string_to_sign = f"POST\n{content_md5}\n{content_type}\n{date}\n{path}"
        sig = hmac.new(
            self.key_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        return b64encode(sig).decode("utf-8")

    # ------------------------------------------------------------------
    # Private helpers — schedule read / parse / build
    # ------------------------------------------------------------------

    def _read_schedule(self) -> dict | None:
        """
        Read the current charge/discharge schedule from the inverter.

        The Solis API returns a 'data' -> 'msg' value string like:
            "50,60,23:30-05:30,00:00-00:00,1,0,00:00-00:00,00:00-00:00,0,0,
             00:00-00:00,00:00-00:00,0,0"
        Format: charge_current, discharge_current, then per slot (4 fields):
            charge_start-charge_end, discharge_start-discharge_end,
            charge_enable, discharge_enable
        """
        result = self._post(self.READ_PATH, {
            "inverterSn": self.inverter_sn,
            "cid":        self.SCHEDULE_CID,
        })
        raw = result.get("data", {}).get("msg", "")
        if not raw:
            uio.warn("Empty schedule response from Solis, using blank defaults.")
            raw = "50,60,00:00-00:00,00:00-00:00,0,0,00:00-00:00,00:00-00:00,0,0,00:00-00:00,00:00-00:00,0,0"
        return self._parse_value_string(raw)

    @staticmethod
    def _parse_value_string(value: str) -> dict:
        """Parse the Solis schedule value string into structured dicts."""

        def split_time(field: str) -> tuple[str, str]:
            if "-" in field:
                s, e = field.split("-", 1)
                return s.strip(), e.strip()
            return "00:00", "00:00"

        parts             = [p.strip() for p in value.split(",")]
        charge_current    = parts[0] if len(parts) > 0 else "50"
        discharge_current = parts[1] if len(parts) > 1 else "60"

        charge    = []
        discharge = []
        for i in range(3):
            base = 2 + i * 4   # skip the two current fields; 4 fields per slot

            c_start, c_end = split_time(parts[base])     if len(parts) > base     else ("00:00", "00:00")
            d_start, d_end = split_time(parts[base + 1]) if len(parts) > base + 1 else ("00:00", "00:00")
            c_enable = int(parts[base + 2]) if len(parts) > base + 2 and parts[base + 2].isdigit() else 0
            d_enable = int(parts[base + 3]) if len(parts) > base + 3 and parts[base + 3].isdigit() else 0

            charge.append(   {"start": c_start, "end": c_end, "enable": c_enable})
            discharge.append({"start": d_start, "end": d_end, "enable": d_enable})

        return {
            "charge_current":    charge_current,
            "discharge_current": discharge_current,
            "charge":            charge,
            "discharge":         discharge,
        }

    @staticmethod
    def _build_value_string(schedule: dict) -> str:
        """Rebuild the Solis value string from structured dicts."""
        parts = [schedule["charge_current"], schedule["discharge_current"]]
        for i in range(3):
            c = schedule["charge"][i]
            d = schedule["discharge"][i]
            parts += [
                f"{c['start']}-{c['end']}",
                f"{d['start']}-{d['end']}",
                str(c["enable"]),
                str(d["enable"]),
            ]
        return ",".join(parts)

    @staticmethod
    def fmt_time(dt: datetime) -> str:
        """Format a datetime as HH:MM in local time for the Solis API."""
        return dt.astimezone().strftime("%H:%M")


# ---------------------------------------------------------------------------
# ChargeSyncApp
# ---------------------------------------------------------------------------
class ChargeSyncApp:
    """
    Orchestrates the sync loop between OctopusClient and SolisClient.
    Polls Octopus for extra dispatch slots and keeps the Solis inverter
    charge schedule in sync.
    """

    def __init__(self, octopus: OctopusClient, solis: SolisClient,
                 poll_interval: int = 180):
        self.octopus       = octopus
        self.solis         = solis
        self.poll_interval = poll_interval
        self._slot_active  = False
        self._active_end:  datetime | None = None

    def run(self) -> None:
        """Start the polling loop. Runs indefinitely."""
        uio.info("=== Octopus -> Solis Intelligent Charging Sync starting ===")
        uio.info(f"Account: {self.octopus.account_number} | Inverter: {self.solis.inverter_sn} | Slot: {self.solis.time_slot} | Poll: {self.poll_interval}s")
        while True:
            self._poll()
            time.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        """Single poll iteration."""
        dispatch = self.octopus.find_active_extra_dispatch()

        if dispatch:
            self._handle_active_dispatch(dispatch)
        else:
            self._handle_no_dispatch()

    def _handle_active_dispatch(self, dispatch: dict) -> None:
        end = dispatch["end"]

        if not self._slot_active:
            uio.info(f'Extra dispatch detected: {self.solis.fmt_time(dispatch["start"])} -> {self.solis.fmt_time(end)}')
            if self.solis.set_charge_slot(dispatch["start"], end):
                self._slot_active = True
                self._active_end  = end

        elif self._active_end != end:
            uio.info(f"Dispatch end time changed to {self.solis.fmt_time(end)}, updating Solis.")
            if self.solis.set_charge_slot(dispatch["start"], end):
                self._active_end = end

        else:
            uio.debug(f"Dispatch still active until {self.solis.fmt_time(end)}.")

    def _handle_no_dispatch(self) -> None:
        if self._slot_active:
            uio.info("No active extra dispatch — clearing Solis charge slot.")
            if self.solis.clear_charge_slot():
                self._slot_active = False
                self._active_end  = None
        else:
            uio.debug("No extra dispatch. Sleeping.")

    @staticmethod
    def create_template_env_file():
        home = Path.home()
        template_env_file = os.path.join(home, 'charge_sync_app.env')
        if os.path.isfile(template_env_file):
            raise Exception(f'{template_env_file} file already present.')

        lines = []
        lines.append(f'OCTOPUS_API_KEY=sk_live_XXXXXXXXXXXXXXXXXXXXXXXX{os.linesep}')
        lines.append(f'OCTOPUS_ACCOUNT_NO=XXXXXXXXXX{os.linesep}')
        lines.append(f'SOLIS_KEY_ID=XXXXXXXXXXXXXXXXXXX{os.linesep}')
        lines.append(f'SOLIS_KEY_SECRET=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX{os.linesep}')
        lines.append(f'SOLIS_INVERTER_SN=XXXXXXXXXXXXXXX{os.linesep}')

        with open(template_env_file, 'w') as fd:
            fd.writelines(lines)

        uio.info(f"Created {template_env_file}")
        uio.info("You should now edit this file to add your Octopus Energy and Solis access details.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """@brief Program entry point"""
    options = None
    try:
        parser = argparse.ArgumentParser(description="A tool to check if your Intelligent Octopus Go account has scheduled an EV charge outside the 23:30-5:30 slot. If this is found to be the case configure your Solis battery system to charge the battery in the scheduled period. This allows use the home battery to be charged during this time on low cost electricity. !!! This tool is only useful to you if you have an EV, are on the Intelligent Octopus Go tariff (used to charge the EV) and have a Solis home storage battery.",
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("-d", "--debug",  action='store_true', help="Enable debugging.")
        parser.add_argument("-e", "--env",    help="The absolute path to the env file containing the Octopus and solis access details.", default=None)
        parser.add_argument("-c", "--create_env_file",  action='store_true', help="Create a template env file. Once created you must manually update this with the Octopus and solis access details.")
        BootManager.AddCmdArgs(parser)

        options = parser.parse_args()

        uio.enableDebug(options.debug)
        uio.logAll(True)
        uio.enableSyslog(True, programName="solis_and_iog")

        prog_version = get_program_version('solis_and_iog')
        uio.info(f"solis_and_iog: V{prog_version}")

        handled = BootManager.HandleOptions(uio, options, True)
        if not handled:

            if options.create_env_file:
                ChargeSyncApp.create_template_env_file()

            else:
                if not options.env:
                    raise Exception("-e/--env command line argument missing.")

                load_dotenv(dotenv_path=options.env)

                octopus = OctopusClient(
                    api_key        = os.getenv("OCTOPUS_API_KEY",    "sk_live_XXXXXXXXXXXXXXXX"),
                    account_number = os.getenv("OCTOPUS_ACCOUNT_NO", "A-XXXXXXXX"),
                )

                solis = SolisClient(
                    key_id      = os.getenv("SOLIS_KEY_ID",      ""),
                    key_secret  = os.getenv("SOLIS_KEY_SECRET",  ""),
                    inverter_sn = os.getenv("SOLIS_INVERTER_SN", ""),
                    api_url     = os.getenv("SOLIS_API_URL",     "https://www.soliscloud.com:13333"),
                    time_slot   = int(os.getenv("TIME_SLOT",     "3")),
                )

                app = ChargeSyncApp(
                    octopus       = octopus,
                    solis         = solis,
                    poll_interval = int(os.getenv("POLL_INTERVAL", "180")),
                )

                app.run()

    # If the program throws a system exit exception
    except SystemExit:
        pass
    # Don't print error information if CTRL C pressed
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        logTraceBack(uio)

        if options and options.debug:
            raise
        else:
            uio.error(str(ex))

if __name__ == "__main__":
    main()
