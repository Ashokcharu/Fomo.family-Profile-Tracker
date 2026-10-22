from __future__ import annotations

import os

from scripts.server import run_server


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    os.environ.setdefault("FOMO_ACTIVITY_MONITOR_ENABLED", "true")
    run_server(host=os.getenv("HOST", "0.0.0.0"), port=port)
