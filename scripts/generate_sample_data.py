"""Generate safe, non-confidential benchmark sample data for the GitHub Pages static site.

This contains synthetic benchmark data modeled after Device 153 energy patterns.
It ensures that the public static GitHub Pages site renders full charts, tables,
KPI cards, and AI diagnostic panels without exposing private historical energy data.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")
SCRIPT_DIR = Path(__file__).resolve().parent
PUBLIC_DATA_DIR = SCRIPT_DIR.parent / "public" / "data"
PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)


def build_sample_dataset() -> dict:
    now = datetime.now(TZ)
    today = now.date()
    yesterday = today - timedelta(days=1)

    # 1. Generate 14 days of realistic daily energy data
    ai_results = []
    daily_totals = []

    # Benchmark pattern around 1450 - 1650 kWh/day
    base_kwh = 1550.0
    for i in range(14, 0, -1):
        day = today - timedelta(days=i)
        is_weekend = day.weekday() >= 5
        day_factor = 0.94 if is_weekend else 1.02
        cycle_var = math.sin(i * 0.7) * 45.0

        actual = round(base_kwh * day_factor + cycle_var, 2)
        predicted = round(actual - (math.cos(i * 0.5) * 18.0), 2)
        err = round(actual - predicted, 2)
        next_pred = round(base_kwh * (0.95 if (day + timedelta(days=1)).weekday() >= 5 else 1.02), 2)

        record = {
            "device_id": 153,
            "date": day.isoformat(),
            "actual_date": day.isoformat(),
            "forecast_date": (day + timedelta(days=1)).isoformat(),
            "processed_at": f"{day.isoformat()}T07:15:00+05:30",
            "evaluated_at": f"{day.isoformat()}T07:15:02+05:30",
            "calendar_profile": "Weekend" if is_weekend else "Weekday",
            "status": "NORMAL",
            "data_status": "complete",
            "actual_kwh": actual,
            "previous_prediction_kwh": predicted,
            "prediction_error_kwh": err,
            "prediction_kwh": next_pred,
            "prediction_lower_kwh": round(next_pred * 0.94, 2),
            "prediction_upper_kwh": round(next_pred * 1.06, 2),
            "base_prediction_kwh": round(next_pred * 0.98, 2),
            "correction_kwh": round(next_pred * 0.02, 2),
            "confidence_inside": True,
            "model_version": "device153-random-forest-daily-v1",
        }
        ai_results.append(record)
        daily_totals.append({
            "device_id": 153,
            "date": day.isoformat(),
            "total_kwh": actual,
        })

    # 2. Generate 24-hour hourly curve for the latest day
    hourly_predictions = []
    # Typical daily load curve: lower at night, peaks morning (08:00) and evening (19:00)
    hourly_curve_factors = [
        0.55, 0.48, 0.45, 0.44, 0.46, 0.52, 0.75, 1.15,
        1.35, 1.25, 1.10, 1.05, 0.98, 0.95, 0.92, 0.98,
        1.10, 1.25, 1.40, 1.45, 1.30, 1.10, 0.85, 0.65
    ]
    current_hour = now.hour

    for h in range(24):
        pred_h = round(64.5 * hourly_curve_factors[h], 2)
        # Completed hours have actual recorded readings
        if h <= current_hour:
            actual_h = round(pred_h + (math.sin(h * 0.9) * 2.8), 2)
            err_h = round(actual_h - pred_h, 2)
        else:
            actual_h = None
            err_h = None

        hourly_predictions.append({
            "device_id": 153,
            "date": today.isoformat(),
            "reading_date": today.isoformat(),
            "hour": h,
            "predicted_kwh": pred_h,
            "actual_kwh": actual_h,
            "error_kwh": err_h,
            "feedback_samples": 42,
            "processed_at": f"{today.isoformat()}T{h:02d}:02:00+05:30",
            "model_version": "device153-adaptive-v2",
        })

    # 3. Health & diagnostics payload
    health = {
        "collector_status": "operational",
        "source": "GitHub Pages Static Benchmark",
        "device_id": 153,
        "last_successful_collection": now.strftime("%Y-%m-%d %H:00:00 %z"),
        "last_run": now.isoformat(),
        "api_latency_ms": {"hourly": 12.4, "daily": 16.8},
        "missing_hours": [],
        "confidence_coverage": 98.2,
        "data_quality": "benchmark_valid",
        "model_version": "device153-random-forest-daily-v1",
        "learning_samples": 55,
        "learning_status": "active",
        "dashboard_build": "2026-10-18-pages",
        "generated_at": now.isoformat(),
    }

    improvement_report = {
        "generated_at": now.isoformat(),
        "model_version": "device153-random-forest-daily-v1",
        "mode": "random_forest_daily",
        "status": "promoted",
        "learning_samples": 55,
        "adaptation_enabled": True,
        "guard_reason": "beats_baseline_by_5pct_margin",
        "evaluated_cycles": 14,
        "base_mae_kwh": 58.42,
        "adapted_mae_kwh": 41.85,
        "safety_policy": "chronological_holdout_gate",
        "next_action": "daily_validation_at_0715_IST",
    }

    health_history = [
        {
            "device_id": 153,
            "observed_at": (now - timedelta(hours=i * 2)).isoformat(),
            "collector_status": "operational",
            "data_status": "complete",
            "hourly_latency_ms": 12.0 + i * 0.3,
            "daily_latency_ms": 16.5 + i * 0.2,
            "missing_hours": [],
            "confidence_coverage": 98.2,
            "payload": {},
        }
        for i in range(5)
    ]

    return {
        "generated_at": now.isoformat(),
        "device_id": 153,
        "ai_results": ai_results,
        "hourly_predictions": hourly_predictions,
        "daily_totals": daily_totals,
        "state": {
            "model_version": "device153-random-forest-daily-v1",
            "last_run": now.isoformat(),
            "profiles": {},
        },
        "health": health,
        "improvement_report": improvement_report,
        "health_history": health_history,
    }


def main() -> None:
    data = build_sample_dataset()
    out_file = PUBLIC_DATA_DIR / "sample_dashboard_data.json"
    out_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Generated benchmark sample dataset: {out_file} ({out_file.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
