# Strategy UI

This folder provides a minimal Flask UI to start/stop the `v29_50pt_strategy_main.py` strategy on an Ubuntu EC2 instance.

Quick start (on EC2 Ubuntu):

1. Install Python 3.10+ and create a venv:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
sudo apt update
sudo apt install python3-full python3-venv -y

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. Export any required environment variables (API_KEY, ACCESS_TOKEN) or add a `.env` file next to the script.

There is a `sample.env` template in the repository. Copy it to `.env` and fill in your real values locally (do not commit `.env`):

```bash
# Unix / macOS
cp sample.env .env

# Windows PowerShell
Copy-Item sample.env .env
```

3. Run the Flask app:

```bash
export FLASK_APP=app.py
flask run --host=0.0.0.0 --port=5000
```

On Windows (PowerShell) use:

```powershell
# activate venv first
venv\Scripts\Activate.ps1
$env:FLASK_APP = 'app.py'
python -m flask run --host=0.0.0.0 --port=5000
```

Or in Command Prompt (CMD):

```cmd
venv\Scripts\activate.bat
set FLASK_APP=app.py
python -m flask run --host=0.0.0.0 --port=5000
```

Alternatively you can run the app directly without setting env vars:

```powershell
venv\Scripts\Activate.ps1
python app.py
```

4. Open the UI in browser at `http://<ec2-public-ip>:5000`.

## Automation (paper-trading auto-start and daily history download)

Paper trading can start automatically each trading morning, and the day's option candles can be
downloaded automatically after market close, both via cron on the server. See
[AUTOMATION.md](AUTOMATION.md) for setup, daily routine and troubleshooting.

## Deploying code updates (FileZilla)

Strategy logic lives in `scripts/strategy/` (`engine.py` plus per-variant settings in
`variants/`); `scripts/v28_strategy_main.py` and `scripts/v30_strategy_main.py` are small launchers.

1. On your PC, after the last code change: `venv\Scripts\python.exe scripts\make_manifest.py`
2. Stop the strategy on the server (outside market hours).
3. In FileZilla, upload the **whole `scripts/strategy/` folder** (it includes `MANIFEST.json`) and
   overwrite. Also upload the launchers, `scripts/editable_config.py` or `app.py`/`templates/` if they changed.
4. Start the app and check the strategy log for `[DEPLOY CHECK] version=..., N files OK`.

If a file is missing, out of date, or a leftover file is found, the log lists it and
**LIVE_ORDER refuses to start** (paper and backtest only warn). FileZilla never deletes
files on the server, so remove leftovers by hand. Emergency override: `SKIP_DEPLOY_CHECK=1` in `.env`.
`scripts/tests/` is not needed on the server.

Notes:
- The UI writes `strategy.log` and `strategy.pid` next to the scripts.
- Strategy run logs are written to `STRATEGY_LOG_DIR` when set, or to the repository's `logs/` directory by default. For persistent Azure or Vultr storage, create a writable directory and set `STRATEGY_LOG_DIR=/var/log/rpalgo` in `.env` before starting the app:

```bash
sudo mkdir -p /var/log/rpalgo
sudo chown -R "$USER":"$USER" /var/log/rpalgo
printf '\nSTRATEGY_LOG_DIR=/var/log/rpalgo\n' >> .env
```

- The Flask app launches the strategy as a subprocess using the same Python executable as Flask. For production, consider using `gunicorn` or `systemd` to manage the Flask process.
 - The UI supports specifying `mode` and `date` when starting the strategy. Use the frontend controls or POST to `/api/start` with JSON `{ "mode": "BACKTEST", "date": "2026-06-19" }`.
 - In BACKTEST mode, the Option data selector is passed to each daily run in the selected date range; choosing Downloaded CSV reads option candles from the matching local daily CSV files, including for older dates when those files are available.
 - Weekly option CSV downloads are manual: start them from the “Download NIFTY Weekly Option CSVs” section in the UI.
 - Logs can be viewed at `/api/logs` (returns tail) or the UI at `/`.
