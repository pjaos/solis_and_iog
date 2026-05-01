import hashlib
import hmac
import json
from base64 import b64encode
from datetime import datetime
from email.utils import formatdate
import requests

# ---------------------------------------------------------------------------
# SolisClient
# ---------------------------------------------------------------------------
class SolisClient:
    """
    Wraps the SolisCloud Control API v2.
    Responsible for reading and writing charge time slots on the inverter.
    """

    CONTROL_PATH        = "/v2/api/control"
    READ_PATH           = "/v2/api/atRead"
    INVERTER_DETAIL_PATH = "/v1/api/inverterDetail"
    SCHEDULE_CID        = "103"

    def __init__(self, key_id: str, key_secret: str, inverter_sn: str,
                 api_url: str = "https://www.soliscloud.com:13333",
                 time_slot: int = 3,
                 uio = None):
        self.key_id      = key_id
        self.key_secret  = key_secret
        self.inverter_sn = inverter_sn
        self.api_url     = api_url.rstrip("/")
        self.time_slot   = time_slot
        self._uio        = uio

    def _info(self, msg):
        if self._uio:
            self._uio.info(f"Solis API: {msg}")

    def _error(self, msg):
        if self._uio:
            self._uio.error(f"Solis API: {msg}")

    def _warn(self, msg):
        if self._uio:
            self._uio.warn(f"Solis API: {msg}")

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
            self._error("Cannot read current Solis schedule — aborting slot write.")
            return False

        start_s = self.fmt_time(start)
        end_s   = self.fmt_time(end)

        idx = self.time_slot - 1
        schedule["charge"][idx] = {"start": start_s, "end": end_s, "enable": 1}

        self._info(f"Setting Solis charge slot {self.time_slot}: {start_s} -> {end_s}")
        result = self._post(self.CONTROL_PATH, {
            "inverterSn": self.inverter_sn,
            "cid":        self.SCHEDULE_CID,
            "value":      self._build_value_string(schedule),
        })
        success = result.get("code") == "0" or result.get("success") is True
        if success:
            self._info(f"Solis charge slot {self.time_slot} set successfully.")
        else:
            self._error(f"Solis API returned unexpected response: {result}")
        return success

    def clear_charge_slot(self) -> bool:
        """Zero out the charge time slot used by this script."""
        schedule = self._read_schedule()
        if schedule is None:
            self._error("Cannot read current Solis schedule — aborting slot clear.")
            return False

        idx = self.time_slot - 1
        schedule["charge"][idx] = {"start": "00:00", "end": "00:00", "enable": 0}

        self._info(f"Clearing Solis charge slot {self.time_slot}.")
        result = self._post(self.CONTROL_PATH, {
            "inverterSn": self.inverter_sn,
            "cid":        self.SCHEDULE_CID,
            "value":      self._build_value_string(schedule),
        })
        success = result.get("code") == "0" or result.get("success") is True
        if success:
            self._info(f"Solis charge slot {self.time_slot} cleared.")
        else:
            self._error(f"Solis clear returned unexpected response: {result}")
        return success

    def get_battery_charge_power(self) -> float | None:
        """
        Return the current battery charge power in watts.

        Queries the SolisCloud inverterDetail endpoint, which returns a
        'batteryPower' field in kW.  The sign convention used by Solis is:
            positive  → battery is charging
            negative  → battery is discharging

        Returns the value converted to watts, or None if the API call fails
        or the field is absent from the response.
        """
        result = self._post(self.INVERTER_DETAIL_PATH, {
            "sn": self.inverter_sn,
        })
        data = result.get("data")
        if not data:
            self._warn("inverterDetail returned no data — cannot read battery power.")
            return None
        battery_power_kw = data.get("batteryPower")
        if battery_power_kw is None:
            self._warn("inverterDetail response contains no batteryPower field.")
            return None
        try:
            watts = float(battery_power_kw) * 1000
        except (TypeError, ValueError) as exc:
            self._warn(f"Could not convert batteryPower value {battery_power_kw!r} to float: {exc}")
            return None
        self._info(f"Battery charge power: {watts:.0f} W (raw: {battery_power_kw} kW)")
        return watts

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
            self._error(f"SolisCloud API error ({path}): {exc}")
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
            self._warn("Empty schedule response from Solis, using blank defaults.")
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