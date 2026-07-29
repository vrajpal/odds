# MLB Odds Web UI

React frontend for the MLB Odds API.

The game history view charts line movement over time (SVG, no chart library):
one series per book, with market/outcome filters, a price-vs-line toggle for
run line and totals, clickable legend chips to hide books, and a crosshair
tooltip. The raw table below the chart remains the accessible/exact view.

## Setup

```bash
npm install
npm run dev
```

The dev server runs on `http://localhost:5173` and proxies API calls to `http://localhost:8000`.

## Build

```bash
npm run build
```

Outputs to `dist/`. The FastAPI server automatically serves these files.
