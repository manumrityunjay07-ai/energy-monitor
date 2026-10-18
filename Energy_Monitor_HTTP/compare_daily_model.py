"""Compare the trained Random Forest candidate with the live model on identical holdout dates.

Read-only: does not change PostgreSQL, model artifacts, or collector code.
Run from the project root:
    python .\Energy_Monitor_HTTP\compare_daily_model.py
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date
from pathlib import Path
from statistics import mean

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from db_connection import get_connection
from train_daily_model import DEVICE_ID, feature_row, get_records
from sklearn.ensemble import RandomForestRegressor


def calc_metrics(actual: list[float], predicted: list[float]) -> tuple[float, float]:
    if not actual:
        return float("nan"), float("nan")
    errors = [a - p for a, p in zip(actual, predicted)]
    mae = sum(abs(e) for e in errors) / len(errors)
    rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
    return mae, rmse


def main() -> int:
    records = get_records()
    if len(records) < 20:
        print(f"Not enough daily actuals for validation: {len(records)}")
        return 2

    X, y, target_dates = [], [], []
    for idx in range(7, len(records)):
        day, target = records[idx]
        X.append(feature_row(records[:idx], day))
        y.append(target)
        target_dates.append(day.isoformat())

    split = max(1, min(len(X) - 5, int(len(X) * 0.80)))
    X_train, y_train = X[:split], y[:split]
    X_test, y_test = X[split:], y[split:]
    test_dates = target_dates[split:]

    # Same Random Forest settings used by train_daily_model.py.
    rf = RandomForestRegressor(
        n_estimators=300,
        max_depth=3,
        min_samples_leaf=3,
        max_features=0.9,
        random_state=42,
        n_jobs=-1,
    )
    rf.fit(X_train, y_train)
    rf_test = [max(0.0, float(value)) for value in rf.predict(X_test)]
    persistence_test = [float(row[0]) for row in X_test]

    test_dates_as_dates = [date.fromisoformat(value) for value in test_dates]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT actual_date, actual_kwh, previous_prediction_kwh, model_version
                FROM daily_energy
                WHERE device_id = %s AND actual_date = ANY(%s)
                ORDER BY actual_date
                """,
                (DEVICE_ID, test_dates_as_dates),
            )
            stored = cur.fetchall()

    live_by_date = {
        row_day.isoformat(): (float(actual), float(previous), model_version)
        for row_day, actual, previous, model_version in stored
        if actual is not None and previous is not None
    }

    # Evaluate each method only on dates where an actual historical live forecast exists.
    common = [i for i, day in enumerate(test_dates) if day in live_by_date]
    print(f"Device ID: {DEVICE_ID}")
    print(f"Chronological holdout dates: {len(test_dates)}")
    print(f"Dates with stored live-model predictions: {len(common)}")
    if not common:
        print("Cannot compare on identical dates: previous_prediction_kwh is missing for all holdout dates.")
        print("Do not replace the live model based on the current report alone. Use a shadow/forward test first.")
        return 0

    actual_common = [float(y_test[i]) for i in common]
    rf_common = [rf_test[i] for i in common]
    persistence_common = [persistence_test[i] for i in common]
    live_common = [live_by_date[test_dates[i]][1] for i in common]

    live_mae, live_rmse = calc_metrics(actual_common, live_common)
    rf_mae, rf_rmse = calc_metrics(actual_common, rf_common)
    persistence_mae, persistence_rmse = calc_metrics(actual_common, persistence_common)

    print("\nSAME-DATE HOLDOUT COMPARISON (kWh; lower is better)")
    print(f"Current stored model: MAE={live_mae:.4f}, RMSE={live_rmse:.4f}")
    print(f"Random Forest candidate: MAE={rf_mae:.4f}, RMSE={rf_rmse:.4f}")
    print(f"Previous-reading baseline: MAE={persistence_mae:.4f}, RMSE={persistence_rmse:.4f}")
    if rf_mae < live_mae:
        print(f"Result: Random Forest MAE is {((live_mae - rf_mae) / live_mae * 100):.2f}% lower than the current stored model on these dates.")
    elif rf_mae > live_mae:
        print(f"Result: Random Forest MAE is {((rf_mae - live_mae) / live_mae * 100):.2f}% higher than the current stored model on these dates.")
    else:
        print("Result: MAE is equal on these dates.")

    print("\nDATES USED:")
    for i in common:
        day = test_dates[i]
        actual = actual_common[common.index(i)]
        live = live_by_date[day][1]
        print(f"{day}: actual={actual:.4f}, current_model={live:.4f}, random_forest={rf_test[i]:.4f}, model_version={live_by_date[day][2]}")

    if len(common) < 8:
        print("\nWarning: fewer than 8 comparable dates; treat this result as preliminary.")
    print("\nNo database data, model artifacts, or collector code were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
