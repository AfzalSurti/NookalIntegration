"""
Production entrypoint for the Back to Ease clinic dashboard.

Runs Uvicorn server on loopback (127.0.0.1:8080) by default.
Loads production settings and builds production container.
"""
from __future__ import annotations

import os
import uvicorn

from app.dashboard.app import create_app
from app.dashboard.production import build_production_container
from app.shared.config import get_settings


def main() -> None:
    settings = get_settings()
    container = build_production_container(settings)
    app = create_app(container)

    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))

    print(f"[production] Starting Back to Ease dashboard on http://{host}:{port} (env: {container.environment})")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
