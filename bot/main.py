"""
Compatibility shim — delegates to app.py.

Existing scripts, Docker CMD entries, and systemd units pointing to
'python bot/main.py' continue to work without change.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on the path (bot/ is one level down)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    from app import main
    main()
