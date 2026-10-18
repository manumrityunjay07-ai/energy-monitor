from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "data" / "state.json"
CSV_PATH = HERE / "results" / "hourly_predictions.csv"
FIELDS = [
    "processed_at",
    "device_id",
    "date",
    "hour",
    "predicted_kwh",
    "actual_kwh",
    "error_kwh",
    "feedback_samples",
    "model_version",
]
TZ = ZoneInfo("Asia/Kolkata")


def as_number(value):
    if value in (None, ""):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse_timestamp(value):
    if not value:
        return datetime.now(TZ)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return datetime.now(TZ)


def read_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{field: row.get(field, "") or "" for field in FIELDS} for row in csv.DictReader(handle)]


def key_for(row: dict) -> tuple[str, int, int]:
    return (str(row.get("date", "")), int(row.get("hour", -1)), int(row.get("device_id") or 153))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely fill missing hourly CSV/PostgreSQL values using saved forecast history without replacing existing PostgreSQL values."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Safely fill missing values in PostgreSQL and hourly_predictions.csv. Existing PostgreSQL values take precedence. Without this flag, run a dry-run only.",
    )
    args = parser.parse_args()

    if not STATE_PATH.exists():
        print(f"ERROR: State file not found: {STATE_PATH}")
        return 1

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    profiles = state.get("profiles", {})
    forecasts = state.get("hourly_forecast_history", {})
    processed = state.get("processed", {})
    existing_rows = read_csv(CSV_PATH)

    rows_by_key: dict[tuple[str, int, int], dict] = {}
    for row in existing_rows:
        try:
            rows_by_key[key_for(row)] = row
        except (TypeError, ValueError):
            continue

    changes: list[dict] = []
    changes_by_date: Counter[str] = Counter()
    skipped_no_forecast = []

    for day, forecast in sorted(forecasts.items()):
        if not isinstance(forecast, dict):
            continue
        predictions = forecast.get("predicted_kwh")
        if not isinstance(predictions, list) or len(predictions) != 24:
            continue

        try:
            date.fromisoformat(day)
            device_id = int(processed.get(day, {}).get("device_id", state.get("device_id", 153)))
        except (TypeError, ValueError):
            device_id = 153

        profile = profiles.get(day, {})
        if not isinstance(profile, dict):
            profile = {}
        generated_at = forecast.get("generated_at") or forecast.get("processed_at") or datetime.now(TZ).isoformat()
        model_version = forecast.get("model_version") or state.get("model_version") or "device153-adaptive-v2"
        try:
            feedback_samples = forecast.get("feedback_samples", processed.get(day, {}).get("hourly_feedback_samples", ""))
            feedback_samples = int(feedback_samples) if feedback_samples not in (None, "") else ""
        except (TypeError, ValueError):
            feedback_samples = ""

        for hour in range(24):
            predicted = as_number(predictions[hour])
            if predicted is None:
                continue
            key = (day, hour, device_id)
            old = rows_by_key.get(key)
            row = dict(old) if old else {field: "" for field in FIELDS}
            was_new = old is None
            row.update({
                "device_id": row.get("device_id") or str(device_id),
                "date": row.get("date") or day,
                "hour": row.get("hour") if row.get("hour") not in (None, "") else str(hour),
                "processed_at": row.get("processed_at") or generated_at,
                "model_version": row.get("model_version") or model_version,
                "feedback_samples": row.get("feedback_samples") or feedback_samples,
            })

            # Never overwrite an existing prediction; fill only a missing one from saved state.
            if row.get("predicted_kwh") in (None, ""):
                row["predicted_kwh"] = str(predicted)

            # Fill actual data only when the CSV row lacks it and state has a measured value.
            actual_from_state = as_number(profile.get(str(hour), profile.get(hour)))
            if row.get("actual_kwh") in (None, "") and actual_from_state is not None:
                row["actual_kwh"] = str(actual_from_state)

            actual = as_number(row.get("actual_kwh"))
            final_prediction = as_number(row.get("predicted_kwh"))
            if row.get("error_kwh") in (None, "") and actual is not None and final_prediction is not None:
                row["error_kwh"] = str(actual - final_prediction)

            is_changed = was_new or (
                old is not None and any(str(row.get(f, "")) != str(old.get(f, "")) for f in FIELDS)
            )
            if is_changed:
                rows_by_key[key] = row
                changes.append(row)
                changes_by_date[day] += 1

    for target_day in ("2026-09-10", "2026-09-11"):
        forecast = forecasts.get(target_day)
        predictions = forecast.get("predicted_kwh") if isinstance(forecast, dict) else None
        if not isinstance(predictions, list) or len(predictions) != 24:
            skipped_no_forecast.append(target_day)

    print("Hourly forecast repair report")
    print("Mode:", "APPLY" if args.apply else "DRY RUN (no changes made)")
    print("Existing CSV rows:", len(existing_rows))
    print("CSV rows to add/fill from saved forecast history:", len(changes))
    if changes_by_date:
        for day, count in sorted(changes_by_date.items()):
            print(f"  {day}: {count} hourly rows to add/fill")
    else:
        print("  No recoverable hourly forecast rows were found missing from CSV.")
    if skipped_no_forecast:
        print("No 24-hour saved forecast available (left untouched):", ", ".join(skipped_no_forecast))

    if not args.apply:
        print("Review this report, then rerun with --apply to save changes.")
        return 0

    if not changes:
        print("Nothing to write. No files or database rows changed.")
        return 0

    # Write PostgreSQL first. Its upsert preserves existing values when incoming fields are null.
    sys.path.insert(0, str(HERE.parent))
    try:
        from db_connection import get_connection
    except Exception as exc:
        print(f"ERROR: Could not import project-root db_connection.py: {exc}")
        return 1

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for row in changes:
                    day, hour, device_id = key_for(row)
                    actual = as_number(row.get("actual_kwh"))
                    predicted = as_number(row.get("predicted_kwh"))
                    error = as_number(row.get("error_kwh"))
                    if error is None and actual is not None and predicted is not None:
                        error = actual - predicted
                    feedback = row.get("feedback_samples")
                    feedback = int(feedback) if feedback not in (None, "") else None
                    collected_at = parse_timestamp(row.get("processed_at"))
                    cur.execute(
                        """
                        INSERT INTO hourly_energy (
                            device_id, reading_date, hour, predicted_kwh,
                            actual_kwh, error_kwh, feedback_samples,
                            collected_at, model_version
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (device_id, reading_date, hour) DO UPDATE SET
                            -- Preserve values already present in PostgreSQL; only fill gaps.
                            predicted_kwh = COALESCE(hourly_energy.predicted_kwh, EXCLUDED.predicted_kwh),
                            actual_kwh = COALESCE(hourly_energy.actual_kwh, EXCLUDED.actual_kwh),
                            error_kwh = COALESCE(
                                hourly_energy.error_kwh,
                                CASE
                                    WHEN COALESCE(hourly_energy.actual_kwh, EXCLUDED.actual_kwh) IS NOT NULL
                                     AND COALESCE(hourly_energy.predicted_kwh, EXCLUDED.predicted_kwh) IS NOT NULL
                                    THEN COALESCE(hourly_energy.actual_kwh, EXCLUDED.actual_kwh)
                                         - COALESCE(hourly_energy.predicted_kwh, EXCLUDED.predicted_kwh)
                                    ELSE EXCLUDED.error_kwh
                                END
                            ),
                            feedback_samples = COALESCE(hourly_energy.feedback_samples, EXCLUDED.feedback_samples),
                            collected_at = COALESCE(hourly_energy.collected_at, EXCLUDED.collected_at),
                            model_version = COALESCE(hourly_energy.model_version, EXCLUDED.model_version)
                        """,
                        (
                            device_id,
                            date.fromisoformat(day),
                            hour,
                            predicted,
                            actual,
                            error,
                            feedback,
                            collected_at,
                            row.get("model_version") or None,
                        ),
                    )
    except Exception as exc:
        print(f"ERROR: PostgreSQL repair failed; CSV has not been changed: {exc}")
        return 1

    # Back up the CSV before replacing it. Database upserts are idempotent if rerun.
    if CSV_PATH.exists():
        backup_path = CSV_PATH.with_name(
            f"{CSV_PATH.stem}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{CSV_PATH.suffix}"
        )
        shutil.copy2(CSV_PATH, backup_path)
        print("CSV backup:", backup_path)

    sorted_rows = sorted(
        rows_by_key.values(),
        key=lambda row: (str(row.get("date", "")), int(row.get("hour") or -1), int(row.get("device_id") or 153)),
    )
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in FIELDS} for row in sorted_rows)

    print(f"Saved {len(changes)} recovered hourly rows to PostgreSQL and {CSV_PATH}.")
    print("Existing actual readings and existing predictions were preserved; missing errors were calculated only when both values were available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
