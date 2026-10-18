"""Train and evaluate a daily energy forecasting model from PostgreSQL.

This script is read-only with respect to PostgreSQL. It writes candidate model
artifacts and a validation report under Energy_Monitor_HTTP/models/.
It does NOT replace or modify the live collector/model.

Run from any directory with:
    python "C:\\Antigravity IDE\\Mini Project\\Energy_Monitor_HTTP\\train_daily_model.py"
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEVICE_ID = int(os.getenv("DEVICE_ID", "153"))
MODEL_DIR = SCRIPT_DIR / "models"
_persistent_dir = os.getenv("PERSISTENT_DIR")
if _persistent_dir:
    _persistent_model_dir = Path(_persistent_dir) / "models"
    _persistent_model_dir.mkdir(parents=True, exist_ok=True)
    MODEL_CANDIDATE_PATH = _persistent_model_dir / "daily_energy_candidate.joblib"
    MODEL_APPROVED_PATH = _persistent_model_dir / "daily_energy_model.joblib"
    REPORT_PATH = _persistent_model_dir / "daily_model_training_report.json"
else:
    MODEL_CANDIDATE_PATH = MODEL_DIR / "daily_energy_candidate.joblib"
    MODEL_APPROVED_PATH = MODEL_DIR / "daily_energy_model.joblib"
    REPORT_PATH = MODEL_DIR / "daily_model_training_report.json"

FEATURE_NAMES = [
    "lag_1", "lag_2", "lag_3", "lag_7",
    "mean_last_3", "mean_last_7", "std_last_7",
    "days_since_last_reading",
    "weekday_sin", "weekday_cos", "year_day_sin", "year_day_cos",
]


def get_records() -> list[tuple[date, float]]:
    from db_connection import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT actual_date, actual_kwh
                FROM daily_energy
                WHERE device_id = %s
                  AND actual_date IS NOT NULL
                  AND actual_kwh IS NOT NULL
                ORDER BY actual_date ASC
                """,
                (DEVICE_ID,),
            )
            raw_rows = cur.fetchall()

    records: list[tuple[date, float]] = []
    seen_dates: set[date] = set()
    for raw_day, raw_value in raw_rows:
        try:
            day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day)[:10])
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value < 0:
            continue
        if day in seen_dates:
            raise RuntimeError(
                f"Duplicate actual_date {day} for device {DEVICE_ID}. "
                "Resolve duplicates before training so one day is not counted twice."
            )
        records.append((day, value))
        seen_dates.add(day)

    return records


def feature_row(history: list[tuple[date, float]], target_date: date) -> list[float]:
    """Create features using only observations earlier than target_date."""
    prior = [(day, value) for day, value in history if day < target_date]
    if len(prior) < 7:
        raise ValueError("At least 7 prior observations are required for features")

    last7 = [value for _, value in prior[-7:]]
    last3 = last7[-3:]
    prior_day = prior[-1][0]
    weekday_angle = 2.0 * math.pi * target_date.weekday() / 7.0
    year_day_angle = 2.0 * math.pi * target_date.timetuple().tm_yday / 365.25

    avg7 = sum(last7) / len(last7)
    std7 = math.sqrt(sum((value - avg7) ** 2 for value in last7) / len(last7))
    return [
        last7[-1], last7[-2], last7[-3], last7[0],
        sum(last3) / len(last3), avg7, std7,
        float(max(1, (target_date - prior_day).days)),
        math.sin(weekday_angle), math.cos(weekday_angle),
        math.sin(year_day_angle), math.cos(year_day_angle),
    ]


def metrics(actual: list[float], predicted: list[float]) -> dict[str, float]:
    errors = [float(a) - float(p) for a, p in zip(actual, predicted)]
    if not errors:
        return {"mae_kwh": float("nan"), "rmse_kwh": float("nan")}
    return {
        "mae_kwh": round(sum(abs(error) for error in errors) / len(errors), 6),
        "rmse_kwh": round(math.sqrt(sum(error * error for error in errors) / len(errors)), 6),
    }


def main() -> int:
    try:
        import joblib
        from sklearn.ensemble import RandomForestRegressor, HistGradientBoostingRegressor
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.linear_model import Ridge
    except ImportError as exc:
        print("Required ML package is missing:", exc)
        print("Install using: python -m pip install scikit-learn joblib")
        return 2

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    records = get_records()
    print(f"Device ID: {DEVICE_ID}")
    print(f"Valid daily actuals: {len(records)}")
    if records:
        print(f"Date range: {records[0][0]} to {records[-1][0]}")
    if len(records) < 20:
        print("Not enough history for a meaningful first validation. Need at least 20 valid daily readings.")
        return 2

    # Build each example from earlier actuals only: no same-day or future leakage.
    X: list[list[float]] = []
    y: list[float] = []
    target_dates: list[str] = []
    for idx in range(7, len(records)):
        day, target = records[idx]
        history = records[:idx]
        X.append(feature_row(history, day))
        y.append(target)
        target_dates.append(day.isoformat())

    if len(X) < 12:
        print("Not enough supervised examples after building lag features.")
        return 2

    # Chronological holdout: newest 20% is used only for evaluation.
    split = max(1, min(len(X) - 5, int(len(X) * 0.80)))
    X_train, y_train = X[:split], y[:split]
    X_test, y_test = X[split:], y[split:]
    test_dates = target_dates[split:]
    baseline_test = [row[0] for row in X_test]  # previous recorded actual
    baseline_metrics = metrics(y_test, baseline_test)

    candidates: dict[str, Any] = {
        "ridge": make_pipeline(StandardScaler(), Ridge(alpha=10.0)),
        "random_forest": RandomForestRegressor(
            n_estimators=300,
            max_depth=3,
            min_samples_leaf=3,
            max_features=0.9,
            random_state=42,
            n_jobs=-1,
        ),
        "hist_gradient_boosting": HistGradientBoostingRegressor(
            max_iter=100,
            max_leaf_nodes=7,
            learning_rate=0.05,
            l2_regularization=5.0,
            random_state=42,
        ),
    }

    evaluation: dict[str, dict[str, float]] = {}
    for name, model in candidates.items():
        model.fit(X_train, y_train)
        predictions = [max(0.0, float(value)) for value in model.predict(X_test)]
        evaluation[name] = metrics(y_test, predictions)

    best_name = min(evaluation, key=lambda name: evaluation[name]["mae_kwh"])
    rf_mae = evaluation["random_forest"]["mae_kwh"]
    # Require Random Forest itself to be the best tested family and at least
    # 5% better than the simple previous-reading baseline before promotion.
    minimum_improvement_fraction = 0.05
    rf_is_best_candidate = best_name == "random_forest"
    rf_beats_baseline_by_margin = rf_mae <= baseline_metrics["mae_kwh"] * (1.0 - minimum_improvement_fraction)

    # Read the cutoff of the currently approved artifact. Do not repeatedly
    # replace it with a model trained on the same data.
    current_approved_trained_through = None
    if MODEL_APPROVED_PATH.exists():
        try:
            current_artifact = joblib.load(MODEL_APPROVED_PATH)
            if isinstance(current_artifact, dict) and current_artifact.get("trained_through"):
                current_approved_trained_through = str(current_artifact["trained_through"])[:10]
        except Exception as exc:
            print(f"Warning: existing approved artifact could not be inspected: {exc}")

    newest_actual_date = records[-1][0].isoformat()
    new_data_available = (
        current_approved_trained_through is None
        or newest_actual_date > current_approved_trained_through
    )
    promote = rf_is_best_candidate and rf_beats_baseline_by_margin and new_data_available

    # Build only a Random Forest artifact, because the live collector explicitly
    # validates that model family. Other candidates are used for comparison only.
    rf_final = candidates["random_forest"]
    rf_final.fit(X, y)
    next_day = records[-1][0] + timedelta(days=1)
    latest_feature = feature_row(records, next_day)
    next_prediction = max(0.0, float(rf_final.predict([latest_feature])[0]))
    artifact = {
        "model": rf_final,
        "model_name": "random_forest",
        "feature_names": FEATURE_NAMES,
        "device_id": DEVICE_ID,
        "trained_through": newest_actual_date,
        "training_observations": len(X),
        "validation_method": "chronological 80/20 holdout; one-step-ahead features",
        "validation_metrics": evaluation["random_forest"],
        "baseline_metrics": baseline_metrics,
    }
    joblib.dump(artifact, MODEL_CANDIDATE_PATH)
    if promote:
        # Replace approved artifact only after model and data checks have passed.
        temporary_path = MODEL_APPROVED_PATH.with_suffix(".joblib.tmp")
        joblib.dump(artifact, temporary_path)
        os.replace(temporary_path, MODEL_APPROVED_PATH)
        deployment_status = "promoted_new_random_forest"
    elif not new_data_available:
        deployment_status = "approved_model_preserved_no_new_actual_dates"
    elif not rf_is_best_candidate:
        deployment_status = "approved_model_preserved_random_forest_not_best_candidate"
    else:
        deployment_status = "approved_model_preserved_validation_gate_failed"

    report = {
        "device_id": DEVICE_ID,
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "data_source": "PostgreSQL daily_energy.actual_kwh",
        "records_loaded": len(records),
        "first_actual_date": records[0][0].isoformat(),
        "last_actual_date": records[-1][0].isoformat(),
        "supervised_examples": len(X),
        "train_examples": len(X_train),
        "test_examples": len(X_test),
        "test_dates": test_dates,
        "baseline_previous_observation": baseline_metrics,
        "candidate_models": evaluation,
        "selected_candidate": best_name,
        "candidate_beats_baseline": rf_mae < baseline_metrics["mae_kwh"],
        "random_forest_is_best_candidate": rf_is_best_candidate,
        "random_forest_beats_baseline_by_minimum_margin": rf_beats_baseline_by_margin,
        "required_minimum_improvement_fraction": minimum_improvement_fraction,
        "new_actual_dates_since_approved_model": new_data_available,
        "current_approved_trained_through": current_approved_trained_through,
        "approved_model_promoted_this_run": promote,
        "next_day_forecast_date": next_day.isoformat(),
        "next_day_forecast_kwh": round(next_prediction, 6),
        "candidate_model_path": str(MODEL_CANDIDATE_PATH),
        "approved_model_path": str(MODEL_APPROVED_PATH) if (promote or MODEL_APPROVED_PATH.exists()) else None,
        "deployment_status": deployment_status,
        "important_note": "Random Forest is promoted only if it is the best tested family, beats the previous-reading baseline by at least 5% MAE, and includes at least one newer actual date than the currently approved model. Validation uses chronological one-step-ahead holdout; it is not a guarantee of future accuracy.",
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")

    print("\nVALIDATION RESULTS (kWh; lower is better)")
    print(f"Previous-observation baseline: MAE={baseline_metrics['mae_kwh']:.4f}, RMSE={baseline_metrics['rmse_kwh']:.4f}")
    for name, scores in evaluation.items():
        print(f"{name}: MAE={scores['mae_kwh']:.4f}, RMSE={scores['rmse_kwh']:.4f}")
    print(f"Best tested model family: {best_name}")
    print(f"Random Forest MAE: {rf_mae:.4f}; baseline MAE: {baseline_metrics['mae_kwh']:.4f}")
    print(f"Random Forest best family: {'YES' if rf_is_best_candidate else 'NO'}")
    print(f"At least 5% better than baseline: {'YES' if rf_beats_baseline_by_margin else 'NO'}")
    print(f"New actual dates since approved model: {'YES' if new_data_available else 'NO'}")
    print(f"Forecast for {next_day}: {next_prediction:.4f} kWh (candidate estimate)")
    print(f"Candidate model: {MODEL_CANDIDATE_PATH}")
    print(f"Report: {REPORT_PATH}")
    if promote:
        print(f"Approved Random Forest model updated: {MODEL_APPROVED_PATH}")
    else:
        print(f"Approved model preserved: {MODEL_APPROVED_PATH if MODEL_APPROVED_PATH.exists() else 'none exists'}")
        print(f"Reason: {deployment_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
