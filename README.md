# solis_and_iog

A tool that bridges **Intelligent Octopus Go** and a **Solis battery inverter**.

When Octopus Energy schedules an EV charge *outside* the standard off-peak window (23:30–05:30), this tool detects the extra dispatch slot and automatically programmes the Solis inverter to charge the home battery during that period — letting you take advantage of cheap electricity.

---

## Who is this for?

This tool is only useful if you have **all three** of the following:

- An electric vehicle (EV)
- An **Intelligent Octopus Go** tariff (used to charge the EV)
- A **Solis home storage battery / inverter**

---

## How it works

1. Every 3 minutes (configurable, min every 1 min) the tool queries the **Octopus Energy GraphQL API** for planned dispatches.
2. Any dispatch slot whose start or end falls *outside* the standard 23:30–05:30 off-peak window is treated as an extra intelligent slot.
3. If the tool is currently inside such a slot and no Solis schedule is active, it writes a charge window into **time slot 3** on the Solis inverter (reserved for this purpose, so slots 1 & 2 for your fixed overnight schedule are untouched).
4. When the slot ends, the Solis schedule is automatically cleared.
5. Syslog is updated to detail when schedules are added and removed.

---

## Prerequisites

- Hardware to run the app on. I have tested it on a Raspberry Pi 2 W but it should run on any Linux, windows or MAC machine that meets the python requirements.
- Python **3.11.2** or later
- A **SolisCloud API Key ID and Secret** — request access at the [Solis support portal](https://solis-service.solisinverters.com/en/support/solutions/articles/44002212561)
- An **Octopus Energy API key** — available in your Octopus account dashboard
- **"Allow Grid Charging"** must be enabled on your inverter (via the SolisCloud app or the inverter display)

---

## Installation

The python wheel installer file can be found in the linux folder.

### Using the bundled installer

```bash
python3 install.py solis_and_iog-<version>-py3-none-any.whl
```

This creates a virtual environment, installs all dependencies, and adds a `solis_and_iog` launcher to your PATH.

### Manual installation with pip

```bash
pip install solis_and_iog-<version>-py3-none-any.whl
```

---

## Configuration

All credentials are supplied via a `.env` file. Generate a template with:

```bash
solis_and_iog -c
```

This creates `/home/auser/charge_sync_app.env`. Open it and fill in your details:

```env
OCTOPUS_API_KEY=sk_live_XXXXXXXXXXXXXXXXXXXXXXXX
OCTOPUS_ACCOUNT_NO=A-XXXXXXXXXX
SOLIS_KEY_ID=XXXXXXXXXXXXXXXXXXX
SOLIS_KEY_SECRET=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
SOLIS_INVERTER_SN=XXXXXXXXXXXXXXX
```

### Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `SOLIS_API_URL` | `https://www.soliscloud.com:13333` | SolisCloud API endpoint |
| `TIME_SLOT` | `3` | Inverter charge time slot to use (1–3) |
| `POLL_INTERVAL` | `180` | Polling interval in seconds |

---

## Usage

```bash
solis_and_iog -e /home/auser/charge_sync_app.env
```

### Command-line options

| Flag | Description |
|---|---|
| `-e / --env <path>` | Path to the `.env` file (required). This must be an absolute path. |
| `-c / --create_env_file` | Create a template `.env` file in your home directory |
| `-d / --debug` | Enable verbose debug logging |
| `--enable_auto_start` | Register the tool to start on system boot |
| `--disable_auto_start` | Un-register the tool to start on system boot |
| `--check_auto_start` | Check the running status |

### Running as a service

Use the built-in boot manager to have the tool start automatically:

```bash
solis_and_iog -e /home/auser/charge_sync_app.env --enable_auto_start
```

---

## Project structure

```
solis_and_iog/
├── src/
│   └── solis_and_iog/
│       └── solis_and_iog.py   # Main application
├── install.py                 # Cross-platform installer
├── pyproject.toml
└── README.md
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | HTTP calls to Octopus and SolisCloud APIs |
| `python-dotenv` | Loading credentials from the `.env` file |
| `p3lib` | Logging, boot manager, and CLI utilities |

---

## Security notes

- Keep your `.env` file private — it contains API secrets. Do **not** commit it to version control.
- The SolisCloud API uses HMAC-SHA1 signed requests; your secret is never sent in plain text.

---

## Troubleshooting

**"Failed to fetch Octopus dispatches"**
Check your `OCTOPUS_API_KEY` and `OCTOPUS_ACCOUNT_NO` values and ensure you are on the Intelligent Octopus Go tariff.

**"SolisCloud API error"**
Verify your `SOLIS_KEY_ID`, `SOLIS_KEY_SECRET`, and `SOLIS_INVERTER_SN`. Ensure your API key has control permissions enabled in the SolisCloud portal.

**Inverter not charging during the slot**
Confirm that "Allow Grid Charging" is enabled on the inverter. Also check that `TIME_SLOT` is not conflicting with an existing schedule in slots 1 or 2.

---

## Licence

MIT — see [LICENSE](LICENSE.txt) for details.

---

## Author

Paul Austen — [pjaos@gmail.com](mailto:pjaos@gmail.com)


## Acknowledgements

Development of this project was assisted by [Claude](https://claude.ai) (Anthropic's AI assistant),
which contributed to code review, bug identification, test generation, and this documentation.