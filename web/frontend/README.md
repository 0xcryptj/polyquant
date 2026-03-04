# PolyQuant Chart Embeds (React)

React components for the dashboard charts: TradingView Symbol Overview (BTC/ETH/SOL) and Chart.js Equity chart.

## Build

```bash
cd web/frontend
npm install
npm run build
```

Output goes to `web/static/assets/` (charts.js, charts.css). The main dashboard loads these via the vanilla index.html.

## Dev

Run the FastAPI server (port 8080) and the Vite dev server:

```bash
# Terminal 1: Backend
uvicorn web.app:app --reload --host 0.0.0.0 --port 8080

# Terminal 2: Frontend dev (proxy /api to backend)
cd web/frontend && npm run dev
```

Then open the Vite dev URL (e.g. http://localhost:5173/charts.html) to test the chart embed in isolation.

For full dashboard with React charts, run only the FastAPI server and open http://localhost:8080 — the built charts bundle is loaded from `/static/assets/charts.js`.
