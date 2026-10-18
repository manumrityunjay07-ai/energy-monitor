"""Hosted daily maintenance — collect readings, then train/validate the model.

This replaces run_daily_model_maintenance.ps1 for Linux/Render environments.
It is designed to be invoked by a cron job or Render scheduled task.

Behaviour:
  1. Run the collector (collect_once_postgres_ml.py).
  2. Confirm that "PostgreSQL: data saved successfully" appears in output.
  3. Only then run controlled model training/validation.
  4. The trainer promotes the approved model only when its own validation and
     new-data checks pass — the existing safety gates are preserved.
  5. Writes timestamped logs to persistent storage.
  6. Uses a file-based lock to prevent overlapping runs.

Exit codes:
  0 — both steps succeeded.
  1 — collector failed or did not confirm PostgreSQL save.
  2 — trainer failed.
  3 — another maintenance run is already in progress.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]
import os
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")
SCRIPT_DIR = Path(__file__).resolve().parent
HTTP_DIR = SCRIPT_DIR / "Energy_Monitor_HTTP"
LOG_DIR = Path(os.getenv("PERSISTENT_DIR", str(SCRIPT_DIR))) / "logs"
LOCK_FILE = LOG_DIR / "maintenance.lock"

LOG_DIR.mkdir(parents=True, exist_ok=True)


def now_ist() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %z")


def run_step(label: str, script: str, log) -> tuple[int, str]:
    """Run a Python script and capture output."""
    log.write(f"\n--- {label} started: {now_ist()} ---\n")
    log.flush()
    result = subprocess.run(
        [sys.executable, script],
        cwd=str(HTTP_DIR),
        capture_output=True,
        text=True,
        timeout=300,  # 5-minute safety timeout per step
    )
    combined = result.stdout + result.stderr
    log.write(combined)
    log.write(f"\n--- {label} finished: exit code {result.returncode} ---\n")
    log.flush()
    return result.returncode, combined


def main() -> int:
    log_path = LOG_DIR / "daily_model_maintenance.log"

    # File-based lock to prevent overlapping runs.
    lock_fd = open(LOCK_FILE, "a+")
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        elif msvcrt is not None:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
    except OSError:
        print("Another maintenance run is in progress — exiting.")
        lock_fd.close()
        return 3

    try:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"\n{'=' * 60}\n")
            log.write(f"Energy Monitor maintenance started: {now_ist()}\n")
            log.write(f"Python: {sys.executable}\n")

            # Step 1: collect readings.
            collector = str(HTTP_DIR / "collect_once_postgres_ml.py")
            exit_code, output = run_step("Collector", collector, log)
            if exit_code != 0 or "PostgreSQL: data saved successfully" not in output:
                msg = "ERROR: Collector failed or did not confirm PostgreSQL save. Training skipped."
                log.write(f"\n{msg}\n")
                print(msg)
                return 1

            # Step 2: controlled model training.
            trainer = str(HTTP_DIR / "train_daily_model_controlled.py")
            exit_code, output = run_step("Controlled Training", trainer, log)
            if exit_code != 0:
                msg = f"ERROR: Controlled model training exited with code {exit_code}."
                log.write(f"\n{msg}\n")
                print(msg)
                return 2

            log.write(f"\nEnergy Monitor maintenance finished: {now_ist()}\n")
            log.write(f"{'=' * 60}\n")
            print(f"Maintenance completed successfully at {now_ist()}")
            return 0
    finally:
        if locked:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    lock_fd.seek(0)
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        lock_fd.close()
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
