# Portfolio Monitor

A browser dashboard for a portfolio of matched spot longs and USDT perpetual shorts. The Overview shows account equity, unrealized P&L, estimated funding at the next settlement, and net exposure. Positions pairs the hedge legs and shows both allocation charts. History covers settled funding, daily simulated cash, and a configurable backtest. Data Health shows the quote timestamps and the observations used for valuation.

## Run locally

Use Python 3.10 or later. From the repository root on macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python serve.py --demo --port 8004
```

Open [http://127.0.0.1:8004/](http://127.0.0.1:8004/). Demo mode uses the original synthetic portfolio in `examples/portfolio.json` and generates synthetic quotes and 400 closed UTC days locally. This mode works without an exchange connection after the dependencies are installed. The UI labels the data as **Synthetic demo**.

### Live Bybit data and your own portfolio

Stop the demo process. Run `python serve.py --port 8004` to view live public Bybit quotes for the included sample holdings. No API key is required. To run the app with a portfolio file you already have, set its local path before starting the service:

```bash
export CMCC_PORTFOLIO_PATH=/absolute/path/to/portfolio.json
python serve.py --port 8004
```

The input is a JSON object with a `positions` array. Each row has `asset`, `symbol`, `instrument`, `side`, `quantity`, and `entry_price_usdt`. The included example demonstrates the supported BTC, ETH, SOL, HYPE and USDT structure. The live overview and positions can run as soon as quotes arrive. To also build the history for that file, generate a matching archive before starting the service:

```bash
python scripts/fetch_history.py --days 30 --output-dir data/case-study
python serve.py --port 8004 --research-dir data/case-study
```

History preparation makes public requests for daily closes, settlement rates, and one-minute mark prices. Start it ahead of the demo. Generated data and the SQLite diagnostics live under `data/` by default. Use a distinct `--research-dir` for each portfolio.

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
python -m unittest discover -s tests
node --test tests/test_frontend.mjs
```

The tests use original synthetic fixtures, including incomplete quotes and recovery, funding boundaries, historical correction protection, and backtest reconciliation. To generate seed fixtures with a fixed end date, run `python scripts/generate_demo_data.py`.
