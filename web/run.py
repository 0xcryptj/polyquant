#!/usr/bin/env python3
"""
Run the PolyQuant Web Terminal.

Usage:
    python web/run.py

Or:
    uvicorn web.app:app --reload --host 0.0.0.0 --port 8080

Open: http://localhost:8080
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
    )
