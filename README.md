# Portfolio Monitor

A browser dashboard for a portfolio of matched spot longs and USDT perpetual shorts. The Overview shows account equity, unrealized P&L, estimated funding at the next settlement, and net exposure. Positions pairs the hedge legs and shows both allocation charts. History covers settled funding, daily simulated cash, and a configurable backtest. Open Data Health from the main navigation or the data quality summary on Overview to inspect quote timestamps and the observations used for valuation.

## Run locally

### 1. Download the project

**macOS:** On the GitHub repository page, select **Code → Download ZIP**. Save `portfolio-monitor-main.zip` to Downloads and double-click it in Finder to extract it. Open Terminal and run:

```bash
cd "$HOME/Downloads/portfolio-monitor-main"
ls serve.py requirements.txt
```

If the folder is elsewhere, type `cd `, drag the extracted folder into Terminal, and press Enter. Run `ls serve.py requirements.txt` to confirm you are in the project directory.

**Clone with Git:** If you already have Git, you can run these commands instead from Terminal on macOS or from a Linux shell. On Windows, use the Ubuntu shell described below.

```bash
git clone https://github.com/LU-Chuanyu/portfolio-monitor.git
cd portfolio-monitor
ls serve.py requirements.txt
```

**Windows:** Run the project in [WSL with Ubuntu](https://learn.microsoft.com/en-us/windows/wsl/install). The app uses the Unix-only Python module `fcntl`. Check `wsl -l -v` in PowerShell. If Ubuntu is missing, run `wsl --install -d Ubuntu` in an administrator PowerShell, restart if prompted, then open Ubuntu and create your Linux username and password.

For the ZIP route on Windows, download the ZIP to Downloads, then run this in PowerShell:

```powershell
Set-Location "$HOME\Downloads"
wsl -d Ubuntu
```

In the Ubuntu shell, check `python3 --version` and run the following. If Downloads is in a different location, start WSL from the folder that contains the ZIP.

```bash
python3 -m zipfile -e ./portfolio-monitor-main.zip "$HOME"
cd "$HOME/portfolio-monitor-main"
ls serve.py requirements.txt
```

### 2. Install dependencies and start the demo

Run `python3 --version` in Terminal on macOS or in your Linux shell. Use **Python 3.10 or later**. On macOS, if Python is missing or too old, install it from [python.org](https://www.python.org/downloads/macos/), reopen Terminal, and check the version again. On Ubuntu, if Python is missing or `venv` reports that `ensurepip` is unavailable, run `sudo apt update` followed by `sudo apt install python3 python3-venv`. If Python names a version-specific `venv` package, such as `python3.14-venv`, install that package after refreshing the index.

From the directory containing `serve.py` and `requirements.txt`, run:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python serve.py --demo --port 8004
```

When the terminal prints `Open http://127.0.0.1:8004/`, leave it running and open [http://127.0.0.1:8004/](http://127.0.0.1:8004/) in your browser. With WSL, open the address in your Windows browser. Check for **Portfolio Overview**, **Synthetic demo · USDT**, and **Simulated** in the feed status. Demo mode uses the sample holdings in `examples/portfolio.json` and generates quotes and 400 closed UTC days locally. After dependencies are installed, the demo works offline.

### Live Bybit data and your own portfolio

Press **Ctrl+C** in the terminal to stop the demo, then start the app without `--demo`:

```bash
./.venv/bin/python serve.py --port 8004
```

Refresh the same browser address. The app uses public Bybit quotes to value the sample holdings. No Bybit login or API key is required.

On **Portfolio Overview**, check that **Bybit data** shows **Live** (WebSocket), **REST**, or **REST fallback**, and that **Updated … UTC** advances. **Last usable** or **Stale quotes** indicates an older observation. Follow **Details** to **Data Health** and select a row in **Observation log** to see the spot and perpetual exchange and receive times. **Refresh quotes** reads the latest snapshot available on the local server.

Keep the terminal running while you use the dashboard. Press **Ctrl+C** there when you are done.

#### Use your own portfolio and prepare history

To use a portfolio file you already have, set its local path before starting the service:

```bash
export CMCC_PORTFOLIO_PATH=/absolute/path/to/portfolio.json
./.venv/bin/python serve.py --port 8004
```

The input is a JSON object with a `positions` array. Each row has `asset`, `symbol`, `instrument`, `side`, `quantity`, and `entry_price_usdt`. The included example demonstrates the supported BTC, ETH, SOL, HYPE and USDT structure. The live overview and positions can run as soon as quotes arrive. To also build the history for that file, generate a matching archive before starting the service:

```bash
./.venv/bin/python scripts/fetch_history.py --days 30 --output-dir data/case-study
./.venv/bin/python serve.py --port 8004 --research-dir data/case-study
```

History preparation makes public requests for daily closes, settlement rates, and one-minute mark prices. Start it ahead of the demo. Generated data and the SQLite diagnostics live under `data/` by default. Use a distinct `--research-dir` for each portfolio.

### Troubleshooting

- **`serve.py` or `requirements.txt` cannot be found.** Run `pwd` and `ls serve.py requirements.txt`. Enter the extracted folder that contains both files.
- **`Address already in use`.** Change `--port 8004` to `--port 8006` in the launch command and open the new address printed in the terminal.
- **The browser cannot connect.** Check that the server is still running and use the exact URL printed in the terminal. On Windows, if `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8004/` returns `200` inside Ubuntu, check [WSL localhost forwarding](https://learn.microsoft.com/en-us/windows/wsl/networking#accessing-linux-networking-apps-from-windows-localhost).
- **Demo runs, but Bybit quotes are missing.** Check **Data Health** and run the two requests below from the same shell environment as Python. Each successful response contains `"retCode":0`. If a request times out or returns HTTP `403`, check your network and the [Bybit error codes](https://bybit-exchange.github.io/docs/v5/error). Add `--demo` back to the launch command if you need to present offline.
- **Live prices update, but History is unavailable.** A clean checkout starts without a verified live history archive. Use demo mode to explore the historical views or follow the archive preparation steps above.

```bash
curl -i --max-time 10 'https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT'
curl -i --max-time 10 'https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT'
```

## What the numbers mean

- **Live valuation.** The collector requests spot `lastPrice` and perpetual `markPrice` from Bybit's public REST tickers for each UTC minute, and reads WebSocket quotes between minute samples. The browser reads cached snapshots at the selected cadence. Manual refresh reads immediately. If either pricing source is incomplete, the dashboard retains the last usable snapshot with its timestamp and leaves a gap in the minute equity chart.
- **Equity and exposure.** Starting account equity is the starting cash plus the cost of the spot inventory. Current equity adds the unrealized P&L on all eight legs. Net exposure is spot notional less short perpetual notional, shown in USDT and as a share of account equity.
- **Funding.** A positive funding rate pays the short. The live estimate uses each eligible short's mark notional and latest funding rate. The headline shows the next settlement batch. If settlements are staggered, the overview also shows the total across each contract's next payment. The history chart has 21 completed eight-hour UTC windows and expresses each net cash flow as a percentage of the latest account equity.
- **History and backtest.** Fixed-position history records a daily simulated cash balance including settled funding. The separate strategy backtest displays funding income, opening and closing fees, modeled slippage and price P&L. Slippage is an adjustable cost assumption on traded notional. The simulation excludes margin, borrowing costs, and liquidation.

## Code and data flow

```text
Public tickers → quote validation → portfolio valuation → published snapshot
Historical observations → archive validation → funding windows and backtest
Published data → read-only HTTP API → dashboard
```

| Files | Responsibility |
| --- | --- |
| `market.py`, `bybit_stream.py`, `bybit_public.py` | Read and validate market data. |
| `portfolio_metrics.py` | Value each position and aggregate the account from validated inputs. |
| `live_monitor.py`, `diagnostics.py` | Publish snapshots, record minute equity, and log observations. |
| `history.py`, `history_backfill.py`, `funding_windows.py`, `backtest.py` | Validate historical observations and calculate historical results. |
| `serve.py` | Connect the modules and serve same-origin, read-only API routes. |
| `api.js`, `view-model.js`, `chart.js`, `app.js` | Validate API data, render charts and views, and handle controls. |

The main endpoints are `/api/snapshot`, `/api/funding-windows`, `/api/history`, `/api/funding-history`, `/api/backtest` and `/api/diagnostics`. The server listens only on localhost.

## Test

The browser-module tests use Node.js 20 or later.

```bash
./.venv/bin/python -m unittest discover -s tests
node --test tests/test_frontend.mjs
```

The tests use original synthetic fixtures, including incomplete quotes and recovery, funding boundaries, historical correction protection, and backtest reconciliation. To generate seed fixtures with a fixed end date, run `python scripts/generate_demo_data.py`.
