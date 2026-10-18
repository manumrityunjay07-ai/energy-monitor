"""
train_model.py — Training pipeline for the Energy Monitor AI models.

Fetches historical energy data from the BIT Sathy EMS API, trains two models:
  1. K-Means clustering for anomaly detection (hourly profiles)
  2. Linear Regression for daily energy forecasting

Exports the trained parameters to esp32_parameters.json, which is consumed
by collect_once.py at inference time.

Usage:
    python train_model.py                     # fetch data + train + export
    python train_model.py --days 180          # fetch 180 days of history
    python train_model.py --skip-fetch        # train using cached data only
    python train_model.py --dry-run           # train but don't overwrite params
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

import requests

# ── Configuration ────────────────────────────────────────────────────────────

DEVICE_ID = int(os.getenv("DEVICE_ID", "153"))
TZ = ZoneInfo("Asia/Kolkata")
HOURLY_URL = os.getenv(
    "HOURLY_URL",
    "https://adc.bitsathy.ac.in/2024/ems_dashboard/api/fetch_hourly_energy_consumption.php",
)
DAYWISE_URL = os.getenv(
    "DAYWISE_URL",
    "https://adc.bitsathy.ac.in/2024/ems_dashboard/api/fetch_day_energy_consumption.php",
)

TRAINING_DATA_DIR = Path("data/training")
HOURLY_CACHE = TRAINING_DATA_DIR / "hourly_profiles.json"
DAILY_CACHE = TRAINING_DATA_DIR / "daily_totals.json"
PARAMS_FILE = Path("esp32_parameters.json")
PARAMS_BACKUP = Path("esp32_parameters.backup.json")
TRAINING_REPORT = Path("results/training_report.json")

HOUR_COLUMNS = [
    "00-01", "01-02", "02-03", "03-04", "04-05", "05-06",
    "06-07", "07-08", "08-09", "09-10", "10-11", "11-12",
    "12-13", "13-14", "14-15", "15-16", "16-17", "17-18",
    "18-19", "19-20", "20-21", "21-22", "22-23", "23-00",
]

N_CLUSTERS = 2
MAX_RETRIES = 4

TRAINING_DATA_DIR.mkdir(parents=True, exist_ok=True)
Path("results").mkdir(exist_ok=True)


# ── Helper Functions ─────────────────────────────────────────────────────────

def now_ist() -> datetime:
    return datetime.now(TZ)


def request_json(method: str, url: str, **kwargs) -> dict:
    """Make an HTTP request with retries, return parsed JSON."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(method, url, timeout=30, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("API response was not a JSON object")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(
        f"Request failed after {MAX_RETRIES} attempts: {url}: {last_error}"
    ) from last_error


# ── Step 1: Data Collection ─────────────────────────────────────────────────

def fetch_daily_totals_range(
    start_date: date, end_date: date
) -> dict[str, float]:
    """Fetch day-wise energy totals from the API for a date range."""
    print(f"  Fetching daily totals: {start_date} -> {end_date} ...")
    payload = request_json(
        "POST",
        DAYWISE_URL,
        files={
            "deviceId": (None, str(DEVICE_ID)),
            "fromDate": (None, start_date.isoformat()),
            "toDate": (None, end_date.isoformat()),
        },
    )
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        raise ValueError("Day-wise API data field is not a list")

    totals: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = row.get("custom_day") or row.get("date")
        raw_total = row.get("total_consumption")
        try:
            day = str(raw_date)[:10]
            value = float(raw_total)
            date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            totals[day] = value
    return totals


def fetch_hourly_for_date(target_date: date) -> dict[str, float] | None:
    """Fetch hourly energy profile for a specific date.

    The API may only support fetching 'today's hourly data, so this function
    attempts to use any available date parameter. Returns None if the API
    doesn't support historical hourly data.
    """
    try:
        payload = request_json(
            "POST",
            HOURLY_URL,
            json={"deviceId": DEVICE_ID, "date": target_date.isoformat()},
        )
        rows = payload.get("data", [])
        if not isinstance(rows, list) or not rows:
            return None

        profile: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                hour = int(row["hour"])
                value = float(row["total_energy"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= hour <= 23 and math.isfinite(value) and value >= 0:
                profile[str(hour)] = value
        return profile if len(profile) >= 20 else None
    except Exception:
        return None


def collect_training_data(days: int) -> tuple[dict[str, float], dict[str, dict]]:
    """Collect historical data for training.

    Returns:
        daily_totals: {date_str: total_kwh}
        hourly_profiles: {date_str: {hour_str: kwh}}
    """
    end = date.today()
    start = end - timedelta(days=days)

    print(f"\n{'='*60}")
    print(f"  DATA COLLECTION - Device {DEVICE_ID}")
    print(f"  Range: {start} -> {end} ({days} days)")
    print(f"{'='*60}\n")

    # ── Fetch daily totals (in 60-day chunks to match API limits) ──
    all_daily: dict[str, float] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=60), end)
        chunk = fetch_daily_totals_range(cursor, chunk_end)
        all_daily.update(chunk)
        print(f"    Got {len(chunk)} daily records for {cursor} -> {chunk_end}")
        cursor = chunk_end + timedelta(days=1)
        time.sleep(0.5)

    print(f"\n  Total daily records collected: {len(all_daily)}")

    # ── Fetch hourly profiles ──
    print(f"\n  Fetching hourly profiles (this may take a while)...")
    all_hourly: dict[str, dict] = {}

    # First, load any cached profiles from state.json
    state_file = Path("data/state.json")
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            cached_profiles = state.get("profiles", {})
            for day, profile in cached_profiles.items():
                complete = {
                    str(h): float(profile[str(h)])
                    for h in range(24)
                    if str(h) in profile
                }
                if len(complete) >= 20:
                    all_hourly[day] = complete
            print(f"    Loaded {len(all_hourly)} profiles from cached state.json")
        except (json.JSONDecodeError, KeyError):
            pass

    # Then try fetching from API for dates we don't have
    fetched_count = 0
    for day_str in sorted(all_daily.keys()):
        if day_str in all_hourly:
            continue
        target = date.fromisoformat(day_str)
        profile = fetch_hourly_for_date(target)
        if profile:
            all_hourly[day_str] = profile
            fetched_count += 1
        # Be gentle with the API
        time.sleep(0.3)

    print(f"    Fetched {fetched_count} new hourly profiles from API")
    print(f"    Total hourly profiles available: {len(all_hourly)}")

    # ── Cache the collected data ──
    DAILY_CACHE.write_text(
        json.dumps(all_daily, indent=2, sort_keys=True), encoding="utf-8"
    )
    HOURLY_CACHE.write_text(
        json.dumps(all_hourly, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"\n  Data cached to {TRAINING_DATA_DIR}/")

    return all_daily, all_hourly


def load_cached_data() -> tuple[dict[str, float], dict[str, dict]]:
    """Load previously cached training data."""
    if not DAILY_CACHE.exists():
        raise FileNotFoundError(
            f"No cached data found at {DAILY_CACHE}. Run without --skip-fetch first."
        )
    daily = json.loads(DAILY_CACHE.read_text(encoding="utf-8"))
    hourly = (
        json.loads(HOURLY_CACHE.read_text(encoding="utf-8"))
        if HOURLY_CACHE.exists()
        else {}
    )
    print(f"  Loaded cached data: {len(daily)} daily, {len(hourly)} hourly")
    return daily, hourly


# ── Step 2: Train K-Means Anomaly Detection ──────────────────────────────────

def standardize(
    data: list[list[float]],
) -> tuple[list[list[float]], list[float], list[float]]:
    """Standardize features to zero mean and unit variance."""
    n_features = len(data[0])
    means = [sum(row[f] for row in data) / len(data) for f in range(n_features)]
    scales = []
    for f in range(n_features):
        variance = sum((row[f] - means[f]) ** 2 for row in data) / len(data)
        scales.append(math.sqrt(variance) if variance > 0 else 0.0)

    scaled = []
    for row in data:
        scaled.append([
            (row[f] - means[f]) / scales[f] if scales[f] > 0 else 0.0
            for f in range(n_features)
        ])
    return scaled, means, scales


def kmeans(
    data: list[list[float]], k: int, max_iter: int = 300, seed: int = 42
) -> tuple[list[list[float]], list[int]]:
    """Pure-Python K-Means clustering (no sklearn dependency).

    Uses K-Means++ initialization for better convergence.
    """
    n = len(data)
    n_features = len(data[0])

    # K-Means++ initialization
    import random
    rng = random.Random(seed)
    centers = [data[rng.randint(0, n - 1)][:]]
    for _ in range(1, k):
        distances = []
        for point in data:
            min_dist = min(
                sum((a - b) ** 2 for a, b in zip(point, c)) for c in centers
            )
            distances.append(min_dist)
        total = sum(distances)
        if total == 0:
            centers.append(data[rng.randint(0, n - 1)][:])
            continue
        probs = [d / total for d in distances]
        cumulative = 0.0
        r = rng.random()
        for i, p in enumerate(probs):
            cumulative += p
            if cumulative >= r:
                centers.append(data[i][:])
                break
        else:
            centers.append(data[-1][:])

    labels = [0] * n
    for iteration in range(max_iter):
        # Assign clusters
        changed = 0
        for i, point in enumerate(data):
            distances = [
                sum((a - b) ** 2 for a, b in zip(point, c)) for c in centers
            ]
            new_label = min(range(k), key=distances.__getitem__)
            if new_label != labels[i]:
                changed += 1
            labels[i] = new_label

        if changed == 0:
            print(f"    K-Means converged at iteration {iteration + 1}")
            break

        # Update centers
        for c in range(k):
            cluster_points = [data[i] for i in range(n) if labels[i] == c]
            if cluster_points:
                centers[c] = [
                    sum(p[f] for p in cluster_points) / len(cluster_points)
                    for f in range(n_features)
                ]
    else:
        print(f"    K-Means reached max iterations ({max_iter})")

    return centers, labels


def train_anomaly_model(
    hourly_profiles: dict[str, dict],
) -> tuple[dict, dict]:
    """Train K-Means anomaly detection on complete 24-hour profiles.

    Returns:
        params: dict with cluster centres, scaler params, threshold
        metrics: dict with training metrics
    """
    print(f"\n{'='*60}")
    print(f"  MODEL 1: K-Means Anomaly Detection")
    print(f"{'='*60}\n")

    # Build feature matrix from complete 24-hour profiles
    complete_profiles: list[tuple[str, list[float]]] = []
    for day in sorted(hourly_profiles):
        profile = hourly_profiles[day]
        if all(str(h) in profile for h in range(24)):
            values = [float(profile[str(h)]) for h in range(24)]
            if all(math.isfinite(v) and v >= 0 for v in values):
                complete_profiles.append((day, values))

    n_samples = len(complete_profiles)
    print(f"  Complete 24-hour profiles: {n_samples}")

    if n_samples < N_CLUSTERS + 1:
        print(f"  WARNING: Not enough complete profiles (need at least {N_CLUSTERS + 1})")
        print(f"  Skipping anomaly model training.")
        return {}, {"status": "skipped", "reason": "insufficient data", "samples": n_samples}

    data = [values for _, values in complete_profiles]

    # Standardize
    scaled_data, scaler_mean, scaler_scale = standardize(data)
    print(f"  Standardized {n_samples} x 24 feature matrix")

    # Train K-Means
    print(f"  Training K-Means with k={N_CLUSTERS}...")
    centers, labels = kmeans(scaled_data, N_CLUSTERS)

    # Compute distances to assigned cluster
    distances = []
    for i, point in enumerate(scaled_data):
        dist = math.sqrt(
            sum((a - b) ** 2 for a, b in zip(point, centers[labels[i]]))
        )
        distances.append(dist)

    # Set anomaly threshold: mean + 2 * std of distances
    mean_dist = mean(distances)
    std_dist = pstdev(distances) if len(distances) > 1 else 0.0
    threshold = mean_dist + 2 * std_dist

    # Cluster distribution
    cluster_counts = [sum(1 for l in labels if l == c) for c in range(N_CLUSTERS)]

    print(f"\n  Results:")
    print(f"    Cluster sizes: {cluster_counts}")
    print(f"    Mean distance: {mean_dist:.4f}")
    print(f"    Std distance:  {std_dist:.4f}")
    print(f"    Anomaly threshold: {threshold:.6f}")

    # Identify anomalies in training data
    anomalies = [
        (complete_profiles[i][0], distances[i])
        for i in range(n_samples)
        if distances[i] > threshold
    ]
    print(f"    Anomalies detected in training data: {len(anomalies)}/{n_samples}")
    for day, dist in anomalies:
        print(f"      {day}: distance = {dist:.4f}")

    params = {
        "number_of_clusters": N_CLUSTERS,
        "cluster_centres_scaled": [
            [round(v, 10) for v in center] for center in centers
        ],
        "cluster_scaler_mean": [round(v, 4) for v in scaler_mean],
        "cluster_scaler_scale": [round(v, 10) for v in scaler_scale],
        "anomaly_threshold": round(threshold, 10),
    }

    metrics = {
        "status": "trained",
        "samples": n_samples,
        "cluster_sizes": cluster_counts,
        "mean_distance": round(mean_dist, 6),
        "std_distance": round(std_dist, 6),
        "threshold": round(threshold, 6),
        "training_anomalies": len(anomalies),
    }

    return params, metrics


# ── Step 3: Train Linear Regression Forecasting ─────────────────────────────

def linear_regression(
    X: list[list[float]], y: list[float], ridge_lambda: float = 1e-8
) -> tuple[list[float], float]:
    """Ridge Regression via the normal equation (pure Python, no numpy).

    Solves: β = (XᵀX + λI)⁻¹ Xᵀy

    A tiny ridge penalty (default 1e-8) ensures the matrix is always
    invertible, even when features are collinear (e.g. x4 = mean of
    x1, x2, x3). The intercept column is NOT regularized.
    """
    n = len(X)
    p = len(X[0])

    # Add intercept column
    X_aug = [[1.0] + row for row in X]
    p_aug = p + 1

    # Compute XtX
    XtX = [[0.0] * p_aug for _ in range(p_aug)]
    for i in range(p_aug):
        for j in range(p_aug):
            XtX[i][j] = sum(X_aug[k][i] * X_aug[k][j] for k in range(n))

    # Add ridge penalty to feature columns only (skip intercept at index 0)
    for i in range(1, p_aug):
        XtX[i][i] += ridge_lambda

    # Compute Xty
    Xty = [sum(X_aug[k][i] * y[k] for k in range(n)) for i in range(p_aug)]

    # Solve via Gaussian elimination with partial pivoting
    aug = [XtX[i][:] + [Xty[i]] for i in range(p_aug)]

    for col in range(p_aug):
        # Partial pivoting
        max_row = max(range(col, p_aug), key=lambda r: abs(aug[r][col]))
        aug[col], aug[max_row] = aug[max_row], aug[col]

        pivot = aug[col][col]
        if abs(pivot) < 1e-12:
            raise ValueError(
                f"Near-singular matrix at column {col} even with "
                f"ridge_lambda={ridge_lambda}. Check your data."
            )

        for j in range(col, p_aug + 1):
            aug[col][j] /= pivot

        for row in range(p_aug):
            if row == col:
                continue
            factor = aug[row][col]
            for j in range(col, p_aug + 1):
                aug[row][j] -= factor * aug[col][j]

    solution = [aug[i][p_aug] for i in range(p_aug)]
    intercept = solution[0]
    coefficients = solution[1:]

    return coefficients, intercept


def train_forecast_model(
    daily_totals: dict[str, float],
) -> tuple[dict, dict]:
    """Train Linear Regression for daily energy forecasting.

    Features (for predicting day D):
        - x1: total kWh on day D-1
        - x2: total kWh on day D-2
        - x3: total kWh on day D-3
        - x4: mean of (D-1, D-2, D-3)

    Target: actual kWh on day D.

    Returns:
        params: dict with coefficients, intercept, scaler params
        metrics: dict with training metrics
    """
    print(f"\n{'='*60}")
    print(f"  MODEL 2: Linear Regression Daily Forecast")
    print(f"{'='*60}\n")

    # Build training samples: need 4 consecutive days to make one sample
    sorted_days = sorted(daily_totals.keys())
    X_raw: list[list[float]] = []
    y_raw: list[float] = []
    sample_dates: list[str] = []

    for i in range(3, len(sorted_days)):
        target_day = sorted_days[i]
        # Check that the 3 prior days are actually consecutive
        prior_days = [sorted_days[i - 3], sorted_days[i - 2], sorted_days[i - 1]]
        expected_prior = [
            (date.fromisoformat(target_day) - timedelta(days=d)).isoformat()
            for d in [3, 2, 1]
        ]

        if prior_days != expected_prior:
            continue

        d1 = daily_totals[sorted_days[i - 1]]  # yesterday
        d2 = daily_totals[sorted_days[i - 2]]  # 2 days ago
        d3 = daily_totals[sorted_days[i - 3]]  # 3 days ago
        target = daily_totals[target_day]

        # Filter out unreasonable values (likely partial days or outages)
        if any(v <= 0 for v in [d1, d2, d3, target]):
            continue

        features = [d1, d2, d3, (d1 + d2 + d3) / 3]
        X_raw.append(features)
        y_raw.append(target)
        sample_dates.append(target_day)

    n_samples = len(X_raw)
    print(f"  Training samples (with 3-day lookback): {n_samples}")

    if n_samples < 5:
        print(f"  WARNING: Not enough consecutive daily data (need at least 5 samples)")
        print(f"  Skipping forecast model training.")
        return {}, {"status": "skipped", "reason": "insufficient data", "samples": n_samples}

    # Standardize features
    n_features = 4
    scaler_mean = [
        sum(X_raw[i][f] for i in range(n_samples)) / n_samples
        for f in range(n_features)
    ]
    scaler_scale = [
        math.sqrt(
            sum((X_raw[i][f] - scaler_mean[f]) ** 2 for i in range(n_samples))
            / n_samples
        )
        for f in range(n_features)
    ]

    X_scaled = [
        [
            (X_raw[i][f] - scaler_mean[f]) / scaler_scale[f]
            if scaler_scale[f] > 0
            else 0.0
            for f in range(n_features)
        ]
        for i in range(n_samples)
    ]

    # Train
    print(f"  Training Linear Regression...")
    coefficients, intercept = linear_regression(X_scaled, y_raw)

    # Evaluate on training data
    predictions = [
        intercept + sum(c * x for c, x in zip(coefficients, X_scaled[i]))
        for i in range(n_samples)
    ]
    errors = [y_raw[i] - predictions[i] for i in range(n_samples)]
    abs_errors = [abs(e) for e in errors]
    mae = mean(abs_errors)
    rmse = math.sqrt(mean([e ** 2 for e in errors]))
    mape = mean(
        abs(e / y) * 100
        for e, y in zip(errors, y_raw)
        if y != 0
    )

    # Cross-validation (leave-last-20%-out)
    split = max(5, int(n_samples * 0.8))
    if split < n_samples:
        X_train, X_test = X_scaled[:split], X_scaled[split:]
        y_train, y_test = y_raw[:split], y_raw[split:]
        cv_coef, cv_intercept = linear_regression(X_train, y_train)
        cv_preds = [
            cv_intercept + sum(c * x for c, x in zip(cv_coef, row))
            for row in X_test
        ]
        cv_errors = [y_test[i] - cv_preds[i] for i in range(len(y_test))]
        cv_mae = mean(abs(e) for e in cv_errors)
        cv_rmse = math.sqrt(mean(e ** 2 for e in cv_errors))
    else:
        cv_mae = None
        cv_rmse = None

    print(f"\n  Results (Training Set):")
    print(f"    MAE:  {mae:.2f} kWh")
    print(f"    RMSE: {rmse:.2f} kWh")
    print(f"    MAPE: {mape:.2f}%")
    print(f"    Intercept: {intercept:.6f}")
    print(f"    Coefficients: {[round(c, 6) for c in coefficients]}")
    if cv_mae is not None:
        print(f"\n  Results (Validation Set, last {n_samples - split} samples):")
        print(f"    MAE:  {cv_mae:.2f} kWh")
        print(f"    RMSE: {cv_rmse:.2f} kWh")

    params = {
        "prediction_scaler_mean": [round(v, 4) for v in scaler_mean],
        "prediction_scaler_scale": [round(v, 10) for v in scaler_scale],
        "prediction_coefficients": [round(v, 10) for v in coefficients],
        "prediction_intercept": round(intercept, 6),
    }

    metrics = {
        "status": "trained",
        "samples": n_samples,
        "train_mae_kwh": round(mae, 4),
        "train_rmse_kwh": round(rmse, 4),
        "train_mape_pct": round(mape, 4),
        "val_mae_kwh": round(cv_mae, 4) if cv_mae else None,
        "val_rmse_kwh": round(cv_rmse, 4) if cv_rmse else None,
        "intercept": round(intercept, 6),
        "coefficients": [round(c, 6) for c in coefficients],
        "sample_dates": [sample_dates[0], sample_dates[-1]],
    }

    return params, metrics


# ── Step 4: Export Parameters ────────────────────────────────────────────────

def export_parameters(
    anomaly_params: dict,
    forecast_params: dict,
    dry_run: bool = False,
) -> None:
    """Merge trained parameters and write to esp32_parameters.json."""

    print(f"\n{'='*60}")
    print(f"  PARAMETER EXPORT")
    print(f"{'='*60}\n")

    # Load existing params as a baseline
    existing = {}
    if PARAMS_FILE.exists():
        existing = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
        print(f"  Loaded existing parameters from {PARAMS_FILE}")

    # Merge: trained params override existing
    merged = dict(existing)
    merged["hour_columns"] = HOUR_COLUMNS
    if anomaly_params:
        merged.update(anomaly_params)
        print(f"  [OK] Anomaly model parameters updated")
    else:
        print(f"  [--] Anomaly model parameters unchanged (training skipped)")

    if forecast_params:
        merged.update(forecast_params)
        print(f"  [OK] Forecast model parameters updated")
    else:
        print(f"  [--] Forecast model parameters unchanged (training skipped)")

    if dry_run:
        print(f"\n  DRY RUN - parameters NOT written to disk")
        print(f"  Would write to: {PARAMS_FILE}")
        print(json.dumps(merged, indent=2))
        return

    # Backup existing
    if PARAMS_FILE.exists():
        PARAMS_BACKUP.write_text(
            PARAMS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(f"  Backup saved to {PARAMS_BACKUP}")

    # Write
    PARAMS_FILE.write_text(
        json.dumps(merged, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  [OK] Parameters exported to {PARAMS_FILE}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train Energy Monitor AI models"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=120,
        help="Number of days of history to fetch (default: 120)",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Use cached training data instead of fetching from API",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Train models but don't overwrite esp32_parameters.json",
    )
    args = parser.parse_args()

    started = now_ist()
    print(f"\n{'#'*60}")
    print(f"  Energy Monitor - Model Training Pipeline")
    print(f"  Device: {DEVICE_ID}")
    print(f"  Started: {started.isoformat()}")
    print(f"{'#'*60}")

    # ── Collect Data ──
    if args.skip_fetch:
        daily_totals, hourly_profiles = load_cached_data()
    else:
        daily_totals, hourly_profiles = collect_training_data(args.days)

    if not daily_totals:
        print("\n  [FAIL] No daily data available. Cannot train.")
        sys.exit(1)

    # ── Train Models ──
    anomaly_params, anomaly_metrics = train_anomaly_model(hourly_profiles)
    forecast_params, forecast_metrics = train_forecast_model(daily_totals)

    # ── Export ──
    export_parameters(anomaly_params, forecast_params, dry_run=args.dry_run)

    # ── Training Report ──
    finished = now_ist()
    report = {
        "trained_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 2),
        "device_id": DEVICE_ID,
        "data": {
            "daily_totals_count": len(daily_totals),
            "hourly_profiles_count": len(hourly_profiles),
            "date_range": [
                min(daily_totals.keys()),
                max(daily_totals.keys()),
            ],
        },
        "anomaly_model": anomaly_metrics,
        "forecast_model": forecast_metrics,
        "dry_run": args.dry_run,
    }
    TRAINING_REPORT.write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    print(f"\n{'#'*60}")
    print(f"  Training Complete!")
    print(f"  Duration: {report['duration_seconds']}s")
    print(f"  Report: {TRAINING_REPORT}")
    if not args.dry_run and (anomaly_params or forecast_params):
        print(f"  Parameters: {PARAMS_FILE}")
        print(f"  Backup: {PARAMS_BACKUP}")
    print(f"{'#'*60}\n")


if __name__ == "__main__":
    main()
