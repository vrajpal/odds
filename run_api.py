#!/usr/bin/env python3
"""Run the FastAPI server for MLB odds."""

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv(Path(".env"))

if __name__ == "__main__":
    host = os.environ.get("API_HOST", "127.0.0.1")
    port = int(os.environ.get("API_PORT", "8000"))

    print(f"Starting MLB Odds API on http://{host}:{port}")
    print("Frontend: http://localhost:5173 (after npm install && npm run dev in frontend/)")

    uvicorn.run(
        "mlb_odds.api:app",
        host=host,
        port=port,
        reload=True,
    )
