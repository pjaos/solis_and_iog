"""
solis_and_iog.py
================================
Monitors Octopus Energy Intelligent Go for extra (outside 23:30-05:30) dispatch
slots and mirrors them as charge-time windows on a Solis AC-coupled inverter via
the SolisCloud Control API.

How it works:
    1. Every POLL_INTERVAL seconds the Octopus GraphQL API is queried for
       plannedDispatches.
    2. Contiguous / overlapping dispatch slots are merged into single windows
       (Octopus splits continuous charging periods into 30-minute settlement
       chunks, e.g. 12:30-13:00 + 13:00-13:30 becomes 12:30-13:30).
    3. Any merged window whose start OR end falls outside the standard
       off-peak window (23:30-05:30) is considered an "intelligent" extra slot.
    4. The Solis charge slot is written PROACTIVELY as soon as the window is
       known (active or upcoming), so the inverter's own clock starts and
       stops the charge with no polling-latency gap. The poll loop's job is
       just to keep the written window in sync with Octopus's latest plan.
    5. Sleeps are adaptive: when a window start, internal chunk boundary, or
       window end is nearer than POLL_INTERVAL, the loop wakes a few seconds
       after that boundary to re-verify the plan (these are the moments a
       reschedule is most likely), then clears the slot promptly after the
       window ends.

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
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from p3lib.uio import UIO
from p3lib.helper import logTraceBack
from p3lib.boot_manager import BootManager
from p3lib.helper import get_program_version

from dotenv import load_dotenv
from solis_and_iog.octopus import OctopusClient
from solis_and_iog.solis import SolisClient

# ---------------------------------------------------------------------------
# ChargeSyncApp
# ---------------------------------------------------------------------------
class ChargeSyncApp:
    """
    Orchestrates the sync loop between OctopusClient and SolisClient.
    Polls Octopus for extra dispatch slots and keeps the Solis inverter
    charge schedule in sync.
    """

    # Wake this many seconds after a window start / boundary / end so the
    # Octopus API has had a moment to reflect any reschedule.
    BOUNDARY_WAKE_DELAY_S = 5
    # Never sleep less than this, even when boundaries are imminent.
    MIN_SLEEP_S = 10
    # Solis charge slots are HH:MM only (no date). A window starting >= ~24h
    # away must not be written yet, or the inverter would activate it at the
    # wrong day's occurrence of that HH:MM. Keep a 60s safety margin.
    PROACTIVE_HORIZON_S = 24 * 3600 - 60

    def __init__(self, octopus: OctopusClient, solis: SolisClient,
                 poll_interval: int = 180,
                 uio = None):
        self.octopus       = octopus
        self.solis         = solis
        self.poll_interval = poll_interval
        self._uio          = uio
        # State of what we have written to the inverter (the window may be
        # upcoming rather than active — slots are written proactively).
        self._slot_written  = False
        self._written_start: datetime | None = None
        self._written_end:   datetime | None = None
        # Internal chunk boundaries of the currently-written merged window,
        # used by the adaptive sleep to re-verify the plan at risky moments.
        self._boundaries:    list[datetime] = []

        # Limit the Octopus API usage
        if self.poll_interval < 60:
            self.poll_interval = 60

    def _info(self, msg):
        if self._uio:
            self._uio.info(f"Octopus API: {msg}")

    def _warn(self, msg):
        if self._uio:
            self._uio.warn(f"Octopus API: {msg}")

    def _debug(self, msg):
        if self._uio:
            self._uio.debug(f"Octopus API: {msg}")

    def run(self) -> None:
        """Start the polling loop. Runs indefinitely."""
        self._info("=== Octopus -> Solis Intelligent Charging Sync starting ===")
        self._info(f"Account: {self.octopus.account_number} | Inverter: {self.solis.inverter_sn} | Slot: {self.solis.time_slot} | Poll: {self.poll_interval}s")
        while True:
            self._poll()
            sleep_s = self._next_sleep()
            self._debug(f"Sleeping {sleep_s:.0f}s until next poll.")
            time.sleep(sleep_s)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        """Single poll iteration."""
        dispatch = self.octopus.find_relevant_extra_dispatch()

        if dispatch:
            self._handle_dispatch(dispatch)
        else:
            self._handle_no_dispatch()

    def _handle_dispatch(self, dispatch: dict) -> None:
        """
        Keep the Solis charge slot in sync with the active or upcoming merged
        dispatch window. The slot is written proactively (before the window
        starts) so the inverter's own clock opens and closes the charge with
        no polling-latency gap.
        """
        now   = datetime.now(timezone.utc)
        start = dispatch["start"]
        end   = dispatch["end"]

        # Solis slots carry HH:MM only. A window starting >= ~24h away would
        # match the wrong day's occurrence of that HH:MM if written now, so
        # hold off — the normal polling cadence will pick it up in time.
        if (start - now).total_seconds() >= self.PROACTIVE_HORIZON_S:
            self._info(f"Upcoming dispatch {self.solis.fmt_time(start)} -> {self.solis.fmt_time(end)} "
                       f"is more than 24h away — deferring slot write.")
            if self._slot_written:
                self._clear_written_slot()
            return

        active = start <= now <= end
        state  = "active" if active else "upcoming"

        if not self._slot_written:
            self._info(f"Extra dispatch window detected ({state}): "
                       f"{self.solis.fmt_time(start)} -> {self.solis.fmt_time(end)}")
            if self.solis.set_charge_slot(start, end):
                self._slot_written  = True
                self._written_start = start
                self._written_end   = end
                self._boundaries    = list(dispatch.get("boundaries", []))

        elif self._written_start != start or self._written_end != end:
            self._info(f"Dispatch window changed to {self.solis.fmt_time(start)} -> "
                       f"{self.solis.fmt_time(end)} ({state}), updating Solis.")
            if self.solis.set_charge_slot(start, end):
                self._written_start = start
                self._written_end   = end
                self._boundaries    = list(dispatch.get("boundaries", []))

        else:
            # Window unchanged; refresh boundaries in case chunk joins moved
            # without the overall window changing.
            self._boundaries = list(dispatch.get("boundaries", []))
            self._info(f"Dispatch window unchanged ({state}), "
                       f"{self.solis.fmt_time(start)} -> {self.solis.fmt_time(end)}.")

        if active:
            self._log_battery_charge_power()

    def _handle_no_dispatch(self) -> None:
        if self._slot_written:
            self._info("No active or upcoming extra dispatch — clearing Solis charge slot.")
            self._clear_written_slot()
        else:
            self._info("No extra dispatch. Sleeping.")

    def _clear_written_slot(self) -> None:
        """Clear the Solis slot and reset written-window state on success."""
        if self.solis.clear_charge_slot():
            self._slot_written  = False
            self._written_start = None
            self._written_end   = None
            self._boundaries    = []
            self._log_battery_charge_power()

    def _next_sleep(self) -> float:
        """
        Return how long to sleep before the next poll.

        Normally poll_interval. But when a boundary of the written window
        (its start, an internal chunk join, or its end) is nearer than
        poll_interval, wake BOUNDARY_WAKE_DELAY_S seconds after the earliest
        such boundary instead:

          - just after START:      verify the dispatch still exists (Octopus
                                   may have cancelled it at short notice —
                                   otherwise the battery grid-charges at peak
                                   rate until the next poll noticed).
          - just after a BOUNDARY: verify the remainder of the merged window
                                   still exists (a future chunk can be
                                   shortened or dropped).
          - just after END:        clear the Solis slot promptly.
        """
        if not self._slot_written:
            return self.poll_interval

        now = datetime.now(timezone.utc)
        upcoming: list[float] = []
        for dt in (self._written_start, *self._boundaries, self._written_end):
            if dt is None:
                continue
            delta = (dt - now).total_seconds()
            if delta > 0:
                upcoming.append(delta + self.BOUNDARY_WAKE_DELAY_S)

        candidates = [s for s in upcoming if s < self.poll_interval]
        if candidates:
            return max(min(candidates), self.MIN_SLEEP_S)
        return self.poll_interval

    def _log_battery_charge_power(self) -> None:
        """Fetch and log the current battery charge power from the Solis inverter."""
        try:
            watts = self.solis.get_battery_charge_power()
            if watts is not None:
                direction = "charging" if watts > 0 else ("discharging" if watts < 0 else "idle")
                self._info(f"Battery charge power: {watts:.0f} W ({direction})")
        except Exception as exc:
            self._warn(f"Could not read battery charge power: {exc}")

    @staticmethod
    def create_template_env_file(uio):
        home = Path.home()
        template_env_file = os.path.join(home, 'solis_and_iog.env')
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
    uio = UIO()
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
                ChargeSyncApp.create_template_env_file(uio)

            else:
                if not options.env:
                    raise Exception("-e/--env command line argument missing.")

                load_dotenv(dotenv_path=options.env)

                octopus = OctopusClient(
                    api_key        = os.getenv("OCTOPUS_API_KEY",    "sk_live_XXXXXXXXXXXXXXXX"),
                    account_number = os.getenv("OCTOPUS_ACCOUNT_NO", "A-XXXXXXXX"),
                    uio            = uio
                )

                solis = SolisClient(
                    key_id      = os.getenv("SOLIS_KEY_ID",      ""),
                    key_secret  = os.getenv("SOLIS_KEY_SECRET",  ""),
                    inverter_sn = os.getenv("SOLIS_INVERTER_SN", ""),
                    api_url     = os.getenv("SOLIS_API_URL",     "https://www.soliscloud.com:13333"),
                    time_slot   = int(os.getenv("TIME_SLOT",     "3")),
                    uio         = uio
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
