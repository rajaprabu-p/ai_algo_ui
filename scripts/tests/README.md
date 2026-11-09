# Backtest snapshot tests

Safety net for the strategy refactor: every refactor step must leave the
backtest trades **identical**.

## Run the check (about 2 minutes)

```powershell
venv\Scripts\python.exe scripts\tests\snapshot.py
```

It ends with `48/48 passed`. Any `FAIL` line shows which trades changed, and
the temp workspace (one `console.log` and Excel per run) is kept for inspection.

## Unit tests (about 20 seconds)

```powershell
venv\Scripts\python.exe -m unittest discover -s scripts\tests -p "test_pure_functions.py" -v
```

Small checks on trailing stops, entry stop price, gap policy, CPR, strike parsing
and the GIFT Nifty helper, run for both v28 and v30. Settings are pinned inside
the tests, so UI changes don't break them. Run everything (unit + snapshot) with
`venv\Scripts\python.exe -m unittest discover -s scripts\tests -v`.

## What the snapshot covers

- v28 and v30, 24 trading days (5 Oct – 6 Nov 2026), BACKTEST mode, DOWNLOADED option data.
- Settings pinned by `fixtures/editable_config.json` (changing the UI settings does not affect the test).
- Spot candles from `fixtures/spot/`; option candles from `data/nifty_options/daily/`. Days missing either are skipped.
- No network and no real keys: `fake_kite/` replaces `kiteconnect`, and scripts are copied to a temp folder without `.env`.

## When results change on purpose

After an intentional strategy change (not a refactor), regenerate and commit the golden files:

```powershell
venv\Scripts\python.exe scripts\tests\snapshot.py --update
```

Keep strategy changes and golden updates in their own commit, separate from refactor commits.

## Adding more days

1. Download the option CSVs for the new days (UI → Download NIFTY Weekly Option CSVs).
2. `venv\Scripts\python.exe scripts\tests\capture_spot_fixtures.py` (needs a valid `ACCESS_TOKEN`).
3. `venv\Scripts\python.exe scripts\tests\snapshot.py --update`, then commit.

The `tests/` folder is not needed on the server; you don't have to upload it.
