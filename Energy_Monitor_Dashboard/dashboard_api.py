from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
HTTP_PROJECT = PROJECT_ROOT / "Energy_Monitor_HTTP"
STATE_PATH = HTTP_PROJECT / "data" / "state.json"
DEVICE_ID = int(os.getenv("DEVICE_ID", "153"))
TZ = ZoneInfo("Asia/Kolkata")

# Import the existing project connection function and keep DB credentials server-side.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from db_connection import get_connection  # noqa: E402


def now_ist() -> str:
    return datetime.now(TZ).isoformat()


def read_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_dashboard_payload() -> dict:
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    device_id,
                    actual_date::text AS date,
                    actual_date::text AS actual_date,
                    forecast_date::text AS forecast_date,
                    processed_at::text AS processed_at,
                    evaluated_at::text AS evaluated_at,
                    calendar_profile,
                    status,
                    data_status,
                    actual_kwh,
                    previous_prediction_kwh,
                    prediction_error_kwh,
                    prediction_kwh,
                    prediction_lower_kwh,
                    prediction_upper_kwh,
                    base_prediction_kwh,
                    correction_kwh,
                    confidence_inside,
                    model_version
                FROM daily_energy
                WHERE device_id = %s
                ORDER BY actual_date
                """,
                (DEVICE_ID,),
            )
            ai_results = cur.fetchall()

            cur.execute(
                """
                SELECT
                    device_id,
                    reading_date::text AS date,
                    hour,
                    predicted_kwh,
                    actual_kwh,
                    error_kwh,
                    feedback_samples,
                    collected_at::text AS processed_at,
                    model_version
                FROM hourly_energy
                WHERE device_id = %s
                ORDER BY reading_date, hour
                """,
                (DEVICE_ID,),
            )
            hourly_predictions = cur.fetchall()

            cur.execute(
                """
                SELECT
                    observed_at::text AS observed_at,
                    collector_status,
                    data_status,
                    hourly_latency_ms,
                    daily_latency_ms,
                    missing_hours,
                    confidence_coverage,
                    payload
                FROM health_events
                WHERE device_id = %s
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (DEVICE_ID,),
            )
            latest_health = cur.fetchone()

    # Keep the same data contract as results/dashboard_data.json.
    daily_totals = [
        {
            "device_id": row.get("device_id", DEVICE_ID),
            "date": row.get("date"),
            "total_kwh": row.get("actual_kwh"),
        }
        for row in ai_results
        if row.get("actual_kwh") is not None
    ]

    health = {}
    if latest_health:
        payload = latest_health.get("payload")
        if isinstance(payload, dict):
            health.update(payload)
        health.setdefault("collector_status", latest_health.get("collector_status"))
        health.setdefault("data_status", latest_health.get("data_status"))
        health.setdefault("observed_at", latest_health.get("observed_at"))
        health.setdefault("api_latency_ms", {
            "hourly": latest_health.get("hourly_latency_ms"),
            "daily": latest_health.get("daily_latency_ms"),
        })
        health.setdefault("missing_hours", latest_health.get("missing_hours") or [])
        health.setdefault("confidence_coverage", latest_health.get("confidence_coverage"))
    health["source"] = "PostgreSQL local API"
    generated_at = now_ist()
    health["generated_at"] = generated_at

    return {
        "generated_at": generated_at,
        "device_id": DEVICE_ID,
        "ai_results": ai_results,
        "hourly_predictions": hourly_predictions,
        "daily_totals": daily_totals,
        "state": read_state(),
        "health": health,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def _send_json(self, value: dict, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _is_production(self) -> bool:
        return os.getenv("RENDER") == "true" or os.getenv("DATABASE_URL") is not None

    def _error_detail(self, exc: Exception) -> str | None:
        """Return exception detail only during local development."""
        if self._is_production():
            import traceback
            traceback.print_exc()  # server log only
            return None
        return str(exc)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/dashboard-data":
            try:
                self._send_json(get_dashboard_payload())
            except Exception as exc:
                detail = self._error_detail(exc)
                body = {"error": "Could not load dashboard data"}
                if detail:
                    body["detail"] = detail
                self._send_json(body, 500)
            return
        if path == "/api/health":
            try:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                        cur.execute("SELECT COUNT(*) FROM hourly_energy WHERE device_id = %s", (DEVICE_ID,))
                        hourly_count = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM daily_energy WHERE device_id = %s", (DEVICE_ID,))
                        daily_count = cur.fetchone()[0]
                self._send_json({
                    "status": "ok",
                    "database": "connected",
                    "device_id": DEVICE_ID,
                    "hourly_rows": hourly_count,
                    "daily_rows": daily_count,
                    "time": now_ist(),
                })
            except Exception as exc:
                detail = self._error_detail(exc)
                body: dict = {"status": "error", "database": "unavailable"}
                if detail:
                    body["detail"] = detail
                self._send_json(body, 500)
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()


def main() -> None:
    # Render sets PORT; DASHBOARD_HOST/DASHBOARD_PORT are kept for local dev.
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("PORT") or os.getenv("DASHBOARD_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Energy Monitor dashboard: http://{host}:{port}")
    print(f"Database health check:     http://{host}:{port}/api/health")
    print(f"Dashboard data API:        http://{host}:{port}/api/dashboard-data")
    is_prod = os.getenv("RENDER") == "true"
    print("Production mode." if is_prod else "Bound to localhost; Ctrl+C stops the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
