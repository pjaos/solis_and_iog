"""
solis_and_iog.py
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
import os
import time
from datetime import datetime
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

    def __init__(self, octopus: OctopusClient, solis: SolisClient,
                 poll_interval: int = 180,
                 uio = None):
        self.octopus       = octopus
        self.solis         = solis
        self.poll_interval = poll_interval
        self._uio          = uio
        self._slot_active  = False
        self._active_end:  datetime | None = None

        # Limit the Octopus API usage
        if self.poll_interval < 60:
            self.poll_interval = 60

    def _info(self, msg):
        if self._uio:
            self._uio.info(f"Octopus API: {msg}")

    def _debug(self, msg):
        if self._uio:
            self._uio.debug(f"Octopus API: {msg}")

    def run(self) -> None:
        """Start the polling loop. Runs indefinitely."""
        self._info("=== Octopus -> Solis Intelligent Charging Sync starting ===")
        self._info(f"Account: {self.octopus.account_number} | Inverter: {self.solis.inverter_sn} | Slot: {self.solis.time_slot} | Poll: {self.poll_interval}s")
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
            self._info(f'Extra dispatch detected: {self.solis.fmt_time(dispatch["start"])} -> {self.solis.fmt_time(end)}')
            if self.solis.set_charge_slot(dispatch["start"], end):
                self._slot_active = True
                self._active_end  = end

        elif self._active_end != end:
            self._info(f"Dispatch end time changed to {self.solis.fmt_time(end)}, updating Solis.")
            if self.solis.set_charge_slot(dispatch["start"], end):
                self._active_end = end

        else:
            self._debug(f"Dispatch still active until {self.solis.fmt_time(end)}.")

    def _handle_no_dispatch(self) -> None:
        if self._slot_active:
            self._info("No active extra dispatch — clearing Solis charge slot.")
            if self.solis.clear_charge_slot():
                self._slot_active = False
                self._active_end  = None
        else:
            self._debug("No extra dispatch. Sleeping.")

    @staticmethod
    def create_template_env_file(uio):
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
