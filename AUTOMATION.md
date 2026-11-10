# Automation: paper-trading auto-start and daily history download

Two jobs run automatically on the server through `cron`:

| Job | Script | When (IST, Mon–Fri) | What it does |
|---|---|---|---|
| **Paper-trading auto-start** | `scripts/scheduled_paper_start.py` | Every 5 min, 09:00–12:25 | Starts v28 and v30 in `PAPERTRADE_LIVE` once a day, as soon as the Kite token is fresh |
| **Daily history download** | `scripts/daily_download.py` | 16:00, retry at 21:00 | Downloads that day's 1-minute NIFTY option candles for backtesting |
| **Paper vs backtest check** | `scripts/paper_vs_backtest.py` | 16:30, retry at 21:30 | Backtests the day on the downloaded data and compares it with the day's paper trades |

Both skip weekends and the NSE holidays listed in `scripts/market_holidays.txt`.
**LIVE_ORDER is never started automatically.** The auto-start can only start paper trading.

---

## 1. Requirements

- Project on the server at `/root/ai_algo_ui`, with the virtual environment at `venv/bin/python`.
- Server time zone **UTC** (check with `timedatectl | grep "Time zone"`). The cron times below are in UTC.
- **A fresh Kite `ACCESS_TOKEN` every trading day.** Kite tokens expire at 06:00 IST, and Zerodha requires a manual login. Log in from the dashboard (**Open Kite Login**) each morning, ideally before 09:00. Both jobs depend on it.
- These files on the server, in `/root/ai_algo_ui/scripts/`:
  - `scheduled_paper_start.py`
  - `daily_download.py`
  - `nifty_weekly_options_daily_files_only.py` (the downloader `daily_download.py` calls)
  - `market_holidays.txt`

## 2. Setup (one time)

Over SSH, open the crontab:

```bash
crontab -e
```

Paste these lines at the end. Each command must stay on **one line**:

```cron
# NIFTY option candles at 16:00 and 21:00 IST (10:30 and 15:30 UTC), Mon-Fri
30 10 * * 1-5 cd /root/ai_algo_ui && mkdir -p logs && flock -n /tmp/daily_download.lock venv/bin/python scripts/daily_download.py >> logs/daily_download.log 2>&1
30 15 * * 1-5 cd /root/ai_algo_ui && mkdir -p logs && flock -n /tmp/daily_download.lock venv/bin/python scripts/daily_download.py >> logs/daily_download.log 2>&1

# Paper trading auto-start: every 5 min from 09:00 to 12:25 IST (03:30-06:55 UTC), Mon-Fri; starts once after the Kite login
30-55/5 3 * * 1-5 cd /root/ai_algo_ui && mkdir -p logs && flock -n -o /tmp/paper_autostart.lock venv/bin/python scripts/scheduled_paper_start.py v28 v30 >> logs/scheduled_paper_start.log 2>&1
*/5 4-6 * * 1-5 cd /root/ai_algo_ui && mkdir -p logs && flock -n -o /tmp/paper_autostart.lock venv/bin/python scripts/scheduled_paper_start.py v28 v30 >> logs/scheduled_paper_start.log 2>&1

# Paper vs backtest check at 16:30 and 21:30 IST (11:00 and 16:00 UTC), Mon-Fri, after the download
0 11 * * 1-5 cd /root/ai_algo_ui && mkdir -p logs && flock -n /tmp/parity.lock venv/bin/python scripts/paper_vs_backtest.py >> logs/paper_vs_backtest.log 2>&1
0 16 * * 1-5 cd /root/ai_algo_ui && mkdir -p logs && flock -n /tmp/parity.lock venv/bin/python scripts/paper_vs_backtest.py >> logs/paper_vs_backtest.log 2>&1
```

In nano: paste with a right-click or Shift+Insert, then press **Ctrl+O**, **Enter**, **Ctrl+X**.

Confirm the lines were saved, and that cron is running:

```bash
crontab -l                  # should show the 4 job lines above
systemctl is-active cron    # should print: active
```

Test both jobs without changing anything:

```bash
cd /root/ai_algo_ui
venv/bin/python scripts/scheduled_paper_start.py v28 v30 --dry-run
venv/bin/python scripts/daily_download.py --dry-run
```

**If the server time zone is `Asia/Kolkata` instead of UTC**, use IST times in the cron lines: download `0 16` and `0 21`; paper start `0-55/5 9`, `*/5 10-11` and `0-25/5 12`.

---

## 3. Paper-trading auto-start

### How it behaves

| Situation | What happens |
|---|---|
| Weekend or a date in `market_holidays.txt` | Nothing (`market holiday (...): nothing to do`) |
| Before 09:00 or after `NO_NEW_ENTRY_AFTER` (dashboard setting, default 12:00) | Nothing (`outside the start window`) |
| Kite token not refreshed today | Waits (`waiting for today's Kite token`); the next 5-minute run checks again |
| Token fresh, not started yet today | **Starts v28 and v30 in PAPERTRADE_LIVE**, the same as the dashboard's Start button |
| Already started earlier today | Nothing (`already auto-started today`). **A strategy you stop from the dashboard is not restarted that day.** |

- If you log in to Kite **before 09:00**, paper trading starts at **09:00**. If you log in later, it starts within 5 minutes of your login.
- The strategy waits for the 09:15 market open before trading.
- Sessions **stop on their own** at the strategy's forced exit (`FORCE_EXIT`, 14:50).
- Started sessions appear in the dashboard with their log and a working **Stop** button.

### Checking it

```bash
tail -20 /root/ai_algo_ui/logs/scheduled_paper_start.log
```

A good morning looks like this:

```
[... 09:00:01 IST] scheduled paper start check for v28, v30
[... 09:00:03 IST] Kite token OK: Kite token saved 11 Nov 08:47 IST, valid until 12 Nov 06:00 IST.
[... 09:00:03 IST] v28: PAPERTRADE_LIVE started [started:12345]
[... 09:00:03 IST] v30: PAPERTRADE_LIVE started [started:12346]
```

### Options

| Want to… | Do this |
|---|---|
| Run only one strategy | In **both** paper-start cron lines, change `v28 v30` to `v28` (or `v30`) |
| Let it start again today after you stopped it | Delete today's marker: `rm /root/ai_algo_ui/logs/autostart_v28_$(date +%F).done` (use `v30` for v30). It starts on the next 5-minute run. |
| Pause auto-start | Put `#` at the start of the two paper-start lines in `crontab -e` |
| Check without starting anything | `venv/bin/python scripts/scheduled_paper_start.py v28 v30 --dry-run` |

Exit codes: `0` ok or nothing to do · `1` a start failed · `2` waiting for the Kite token.

---

## 4. Daily history download

### What it downloads

Each trading day after market close, it downloads that day's 1-minute candles into `data/nifty_options/daily/nifty_weekly_<expiry>_<trade date>.csv`:

| Day | Expiries downloaded |
|---|---|
| Normal day | The **current** (nearest) expiry only |
| **Expiry day** | The contract **expiring that day** and the **next** expiry |

- Expiries are read from Kite's instrument list each day, so holiday shifts (e.g. 24 Nov → 23 Nov) and monthly expiries are handled automatically.
- Existing complete files are skipped, so the 21:00 run only fills gaps, for example when the token wasn't ready at 16:00.
- Redundant same-day copies of the instrument list in `data/instrument_archives/` are pruned automatically. A copy is removed only if every contract in it is also in that day's newest list.
- **Expired contracts can't be downloaded later.** Don't delete these files, and copy them to your PC regularly (see section 5).

### Checking it

**From the dashboard:** the **Daily Download Status (automatic)** box shows the last run, the files for that day with their sizes, and the last log lines. Click **Refresh**.

| Badge | Meaning |
|---|---|
| 🟢 Last run OK | Files downloaded, or already present |
| 🔴 Kite login failed | That day's token was missing or expired; refresh it and the 21:00 run retries |
| 🔴 Last run FAILED | A download failed or was only partly fetched; the next run retries the missing contracts |
| 🟡 Running / Incomplete | Still running, or a run stopped without finishing |
| ⚪ Skipped | Weekend, holiday or before 15:45 |

**Over SSH:**

```bash
tail -40 /root/ai_algo_ui/logs/daily_download.log
```

The dashboard's **Downloaded NIFTY Option CSV Files** section lists the files by expiry, and can download a single file.

### Options

| Want to… | Command (run from `/root/ai_algo_ui`) |
|---|---|
| See what it would download | `venv/bin/python scripts/daily_download.py --dry-run` |
| Download a missed day (only while its contracts haven't expired, usually the same week) | `venv/bin/python scripts/daily_download.py --date 2026-11-12` |
| Download the 2 nearest expiries every day | `venv/bin/python scripts/daily_download.py --expiries 2` |
| Run before 15:45 or on a listed holiday | add `--force` |

Exit codes: `0` ok or nothing to do · `1` a download failed · `2` Kite login or token problem.

---

## 4b. Paper vs backtest check

After the day's option candles are downloaded, `scripts/paper_vs_backtest.py` backtests that day
(in an isolated temporary copy, so your log folder is not touched) and compares the result with
the day's paper trades for v28 and v30. Paper trades are read from `STRATEGY_LOG_DIR`
(`papertrade_<ddmmyyyy>_*_<variant>.xlsx`, all sessions of the day merged).

Trades are paired by side and entry time (within 3 minutes). For each pair it checks the contract,
exit reason, entry/exit prices (after removing the backtest's slippage, since paper fills at the
signal price; tolerance 1.5 points) and exit time (tolerance 3 minutes).

| Verdict | Meaning |
|---|---|
| **MATCH** | Paper and backtest took the same trades with the same results; the backtest reflects live behaviour |
| **DIFFERENT** | Something differs; each difference is listed. Investigate before trusting backtests for live decisions |
| NO_PAPER_TRADES | The backtest traded but no paper trades were found (paper did not run, or did not trade) |
| NO_TRADES | Neither traded |

A **"SETTINGS DIFFER"** line means the paper session ran with different settings (MTF, MFE exit,
max trades, SL) than the backtest uses today, so the differences may come from the settings.
A note about **restarts** means paper ran several sessions that day; each restart starts a fresh
trade count, which the backtest does not do.

Reports: `logs/parity/parity_<date>.txt` (readable) and `.json`. Log: `logs/paper_vs_backtest.log`.

```bash
venv/bin/python scripts/paper_vs_backtest.py --date 2026-11-11            # a specific day
venv/bin/python scripts/paper_vs_backtest.py --date 2026-11-11 --variant v28
```

Exit codes: `0` MATCH or nothing to compare · `1` DIFFERENT or ERROR · `2` the day's option files are not downloaded yet.

## 5. Weekly: use the new data for backtesting (on your PC)

1. With FileZilla, copy the new files from the server's `data/nifty_options/daily/` to `F:\AI_Trading\ai_algo_ui\data\nifty_options\daily\`. Drag from right to left, and choose **Skip** for files that already exist.
2. Capture the matching NIFTY spot candles (needs a valid token on the PC):
   ```powershell
   venv\Scripts\python.exe scripts\tests\capture_spot_fixtures.py
   ```
3. Add the new days to the backtest snapshots, then commit:
   ```powershell
   venv\Scripts\python.exe scripts\tests\snapshot.py --update --date 2026-11-11 --date 2026-11-12
   ```

---

## 6. Market holidays

`scripts/market_holidays.txt` lists NSE trading holidays, one per line:

```
2026-11-24  # Prakash Gurpurb Sri Guru Nanak Dev
```

- Only weekdays are needed, since weekends are skipped anyway.
- **Each year,** add the next year's list from NSE's official holiday calendar (nseindia.com → Resources → Exchange Communication → Holidays), then upload the file to the server's `scripts/` folder.
- A missed holiday does no harm: that day just shows "Kite login failed" or "No candles returned" instead of "Skipped".

---

## 7. Your daily routine

| Time (IST) | You | Automatic |
|---|---|---|
| Before 09:00 | **Log in to Kite** from the dashboard | — |
| 09:00 | — | Paper trading starts (v28, v30) |
| 09:15–14:50 | Check the dashboard when you like | Strategy trades on paper, stops at 14:50 |
| 16:00 | — | Option candles downloaded |
| 21:00 | — | Retry for anything missed |
| Weekly | Copy the new CSVs to your PC (section 5) | — |

---

## 8. Troubleshooting

| Problem | Check |
|---|---|
| Nothing starts at 09:00 | `crontab -l` shows the paper-start lines? `tail -20 logs/scheduled_paper_start.log`: is it waiting for the token, a holiday, or an error? |
| `waiting for today's Kite token` | Log in to Kite in the dashboard; it starts within 5 minutes (until 12:00) |
| No log file at all | Cron isn't running the job: check `crontab -l`, `systemctl is-active cron`, and that the path is `/root/ai_algo_ui` |
| Strategy started but stopped at once | Open its log in the dashboard; usually an expired or invalid token. (A `[DEPLOY CHECK] PROBLEM` only warns in paper mode; it blocks LIVE_ORDER only.) |
| Download badge red: Kite login failed | The token wasn't refreshed that day. Refresh it before 21:00 to catch the retry; after the contracts expire the day can't be recovered |
| Download badge red: FAILED | `tail -40 logs/daily_download.log`; the next run retries the missing contracts automatically |
| A file is much smaller than usual | Normal files are about 6–11 MB. Check the log for request errors |
| Disk filling up | `df -h /root`; option data grows about 2 GB a year |

## 9. After changing these scripts

Upload the changed file(s) to `/root/ai_algo_ui/scripts/` with FileZilla. **No Flask restart is needed**, because cron starts a fresh process for every run. These scripts aren't part of the strategy's deploy check (`MANIFEST.json`).
