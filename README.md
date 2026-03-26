# ib_chart

A lightweight local web app (Flask + HTML) that gives you fast, trader-friendly charts backed by Interactive Brokers (IBKR).

This is designed for US stock traders who may not code, but want to **connect charts to their screener and dashboard** (and use “vibe coding” with an AI assistant to glue things together).

## Screenshots

### Single chart
![Single chart screenshot](assets/ScreenShot1.png)

### Multi-chart
![Multi-chart screenshot](assets/ScreenShot2.png)

## What you get (in plain English)

- **A single chart page**: `ib_chart.html`
- **A multi-panel chart page**: `ib_multichart.html` (great for watchlists and theme baskets)

- **Simple APIs** your screener/dashboard can call:
  - **Daily candles**: `GET /api/pricehistory?symbol=AAPL&period_str=1y&hist_source=local|ib`
  - **Intraday candles (1m/5m)**: `GET /api/intraday?symbol=AAPL&interval=1m`
  - **Intraday live stream** (updates every second): `GET /api/intraday/stream?symbol=AAPL`
  - **Quote**: `GET /api/quote?symbol=AAPL`
  - **EPS/Revenue table** (for the UI): `GET /api/eps-revenue?symbol=AAPL&quarters=5`

## The mental model

- This runs on **your own computer** (localhost).
- Your browser loads the chart pages from `http://127.0.0.1:5001`.
- The chart pages call the APIs above, and the server pulls data from:
  - **IBKR realtime** (for intraday)
  - **IBKR historical** or **your local CSV daily database** (for daily candles)

If you already have a screener or dashboard page, you can treat `ib_chart` as your local “chart microservice”.

## Requirements

- **Python**: 3.10+ recommended (works fine on Windows)
- **IBKR**: TWS or IB Gateway running with API enabled
- **Network**: your machine must be able to reach IB Gateway/TWS (usually localhost)

## Install (Windows quick start)

Open PowerShell in this folder (`ib_chart/`) and run:

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Configure (optional but recommended)

### 1) IB connection settings

Set these if your IB Gateway/TWS is not on the defaults:

- `IB_HOST` (default `127.0.0.1`)
- `IB_PORT` (default `4001`)
- `IB_CLIENT_ID` (default `1001`)

Example (PowerShell):

```bash
$env:IB_HOST="127.0.0.1"
$env:IB_PORT="4001"
$env:IB_CLIENT_ID="1001"
```

### 2) Local daily CSV directory (optional)

If you have local daily bars in CSV form, set:

- `IB_CHART_LOCAL_DAILY_DIR`

Default is:

- `D:\US_stocks_daily_data\listed stocks from 2000_cleaned`

Example:

```bash
$env:IB_CHART_LOCAL_DAILY_DIR="D:\US_stocks_daily_data\listed stocks from 2000_cleaned"
```

## Run

In the activated venv:

```bash
python ib_server.py
```

Then open in your browser:

- `http://127.0.0.1:5001/` (serves the chart UI)
- `http://127.0.0.1:5001/ib_chart.html`
- `http://127.0.0.1:5001/ib_multichart.html`

## Using this with your screener + dashboard (no-code / vibe coding friendly)

### Option A: “Just open a chart for one symbol”

Your dashboard can open a new tab like:

- `http://127.0.0.1:5001/ib_chart.html`

(Then type the symbol in the UI.)

### Option B: MultiChart from a screener/watchlist (recommended)

If your screener produces a list of symbols (e.g. the top movers), you can send them to MultiChart via URL:

- `http://127.0.0.1:5001/ib_multichart.html?symbols=AAPL,MSFT,NVDA,TSLA&tf=D`

Notes:

- `symbols` is a comma-separated list
- `tf` can be `D`, `W`, or `M`
- MultiChart has “Sync symbol” and “Sync D/W/M” toggles in the top bar

### Option C: Embed charts inside your own dashboard page

If your dashboard is a local HTML page, you can embed the chart page as an iframe:

```html
<iframe
  src="http://127.0.0.1:5001/ib_multichart.html?symbols=AAPL,MSFT,NVDA,TSLA&tf=D"
  style="width:100%;height:900px;border:0;"
></iframe>
```

If you’re using an AI assistant to “vibe code” the dashboard, tell it:

- you want an iframe block like above
- your screener output is a list of symbols
- you want to generate the `symbols=` query string dynamically

## API examples (for automation)

### Daily candles

Local CSV source:

- `GET /api/pricehistory?symbol=AAPL&period_str=1y&hist_source=local`

IB historical source:

- `GET /api/pricehistory?symbol=AAPL&period_str=1y&hist_source=ib`

### Intraday

- `GET /api/intraday?symbol=AAPL&interval=1m`
- `GET /api/intraday?symbol=AAPL&interval=5m`

### Quote

- `GET /api/quote?symbol=AAPL`

## Troubleshooting (common trader issues)

### “IB connect failed” / no intraday data

- Make sure **IB Gateway or TWS is running**
- Make sure **API is enabled** in TWS/Gateway settings
- Check the port:
  - IB Gateway paper often uses `4002`
  - IB Gateway live often uses `4001`
- If you run multiple tools, change `IB_CLIENT_ID` to something unique

### “No local daily data”

- Either set `IB_CHART_LOCAL_DAILY_DIR` to your CSV folder, or use `hist_source=ib`
- The local CSV format is expected to have columns like `DateTime, Open, High, Low, Close, Volume`

### EPS/Revenue endpoint is slow

- It fetches from public data sources and may take a few seconds the first time.
- Results are cached for UI responsiveness.

## Safety & privacy

- This server is intended for **localhost** usage.
- Do not expose it directly to the public internet unless you know what you’re doing.
- This project is not financial advice.

## Third-party data and licenses

- This project depends on third-party services/libraries, including IBKR APIs, `yfinance`, and `finvizfinance`.
- Market data providers and websites each have their own Terms of Service. You are responsible for using data in compliance with those terms.
- Frontend assets loaded from public CDNs (such as ECharts and Lightweight Charts) remain subject to their original licenses.

## License

MIT. See `LICENSE`.