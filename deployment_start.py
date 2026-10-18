"""Render web service startup — initialize persistent storage, then start dashboard.

On the first deployment, this copies the approved model artifact from the repo
to the persistent disk.  On subsequent deployments, the existing persistent
copy is preserved (it may have been promoted by the cron job).

After initialization, this exec's the dashboard server.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PERSISTENT_DIR = Path(os.getenv("PERSISTENT_DIR", str(SCRIPT_DIR)))
REPO_MODEL_DIR = SCRIPT_DIR / "Energy_Monitor_HTTP" / "models"
PERSISTENT_MODEL_DIR = PERSISTENT_DIR / "models"
PERSISTENT_LOG_DIR = PERSISTENT_DIR / "logs"


def initialize_persistent_storage() -> None:
    """Copy the approved model to persistent storage if it does not exist there."""
    PERSISTENT_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    PERSISTENT_LOG_DIR.mkdir(parents=True, exist_ok=True)

    approved = REPO_MODEL_DIR / "daily_energy_model.joblib"
    target = PERSISTENT_MODEL_DIR / "daily_energy_model.joblib"

    if approved.exists() and not target.exists():
        shutil.copy2(str(approved), str(target))
        print(f"Initialized persistent model: {target} ({approved.stat().st_size} bytes)")
    elif target.exists():
        print(f"Persistent model already exists: {target} ({target.stat().st_size} bytes)")
    else:
        print(f"Warning: no approved model found at {approved}")


def main() -> None:
    initialize_persistent_storage()

    # Hand off to the dashboard server.
    dashboard = str(SCRIPT_DIR / "Energy_Monitor_Dashboard" / "dashboard_api.py")
    print(f"Starting dashboard: {dashboard}")
    os.execv(sys.executable, [sys.executable, dashboard])


if __name__ == "__main__":
    main()
