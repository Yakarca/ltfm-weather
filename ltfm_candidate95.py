#!/usr/bin/env python3
"""Hour-specific NOAA-verified rolling point correction for the v2.1 LTFM engine."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import statistics
from collections import defaultdict

import numpy as np
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.impute import SimpleImputer

import ltfm_engine_v2 as engine

ROOT = pathlib.Path(__file__).resolve().parent
SEED_PATH = ROOT / "data" / "candidate95_seed.json"
LIVE_PATH = ROOT / "data" / "candidate95_live.json"
HISTORY_WINDOW = 60
MAX_CORRECTION_C = 0.5
FORECAST_SLOTS = ("00", "03", "06", "09", "12", "15", "18", "21")
SEED_SLOTS = ("09", "15", "22")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def round_noaa(value):
    return math.floor(float(value) + 0.5)


def model():
    return ExtraTreesRegressor(
        n_estimators=50,
        min_samples_leaf=7,
        max_features=0.7,
        random_state=29,
        n_jobs=-1,
    )


def train_predict(history, current, feature_keys=None):
    """Fit only on completed NOAA days from the same forecast issue hour."""
    current_issue = current["issue_date"]
    current_slot = str(current.get("slot", ""))
    rows = [
        row for row in history
        if str(row.get("slot", "")) == current_slot
        if row.get("issue_date", "") < current_issue
        and row.get("target_date", "") < current_issue
        and number(row.get("actual_c"))
        and number(row.get("base"))
    ]
    rows.sort(key=lambda row: (row.get("target_date", ""), row.get("issue_date", "")))
    rows = rows[-HISTORY_WINDOW:]
    if len(rows) < HISTORY_WINDOW:
        return None, len(rows)

    if feature_keys is None:
        feature_keys = sorted(
            set(current.get("features", {})).union(
                *(set(row.get("features", {})) for row in rows)
            )
        )
    x_train = np.asarray(
        [[
            float(row.get("features", {}).get(key))
            if number(row.get("features", {}).get(key)) else np.nan
            for key in feature_keys
        ] for row in rows],
        dtype=float,
    )
    x_current = np.asarray(
        [[
            float(current.get("features", {}).get(key))
            if number(current.get("features", {}).get(key)) else np.nan
            for key in feature_keys
        ]],
        dtype=float,
    )
    imputer = SimpleImputer(strategy="median", keep_empty_features=True)
    x_train = imputer.fit_transform(x_train)
    x_current = imputer.transform(x_current)
    keep = x_train.std(axis=0) > 1e-9
    residuals = np.asarray([row["actual_c"] - row["base"] for row in rows], dtype=float)
    if not keep.any():
        # If a quiet 60-day window has no varying predictors, use a robust
        # same-hour bias estimate instead of silently skipping calibration.
        correction = float(statistics.median(residuals))
        return float(np.clip(correction, -MAX_CORRECTION_C, MAX_CORRECTION_C)), len(rows)

    estimator = model()
    estimator.fit(x_train[:, keep], residuals)
    correction = float(estimator.predict(x_current[:, keep])[0])
    return float(np.clip(correction, -MAX_CORRECTION_C, MAX_CORRECTION_C)), len(rows)


def corrected_integer(base, correction):
    """Apply a symmetric whole-degree offset to an integer base forecast."""
    main = round_noaa(base)
    if correction is None:
        return main
    if correction >= MAX_CORRECTION_C:
        return main + 1
    if correction <= -MAX_CORRECTION_C:
        return main - 1
    return main


def metrics(errors):
    n = len(errors)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "hits": sum(error == 0 for error in errors),
        "exact_accuracy_pct": round(100 * sum(error == 0 for error in errors) / n, 2),
        "mae_c": round(sum(abs(error) for error in errors) / n, 3),
        "within_1c_pct": round(100 * sum(abs(error) <= 1 for error in errors) / n, 2),
    }


def _seed_history(seed):
    """Build only hour-matched, time-available features from the legacy seed."""
    history = []
    for source in sorted(seed.get("history", []), key=lambda row: row["issue_date"]):
        source_features = source.get("features", {})
        for slot in SEED_SLOTS:
            if slot == "22":
                base = source.get("base")
                features = dict(source_features)
                if number(base):
                    features["d1_current"] = float(base)
            else:
                base = source_features.get(f"d1_{slot}")
                features = {
                    "base": float(base) if number(base) else np.nan,
                    "d1_current": float(base) if number(base) else np.nan,
                }
                d0 = source_features.get(f"d0_{slot}")
                if number(d0):
                    features["d0_current"] = float(d0)
                for key in ("season_sin", "season_cos"):
                    if number(source_features.get(key)):
                        features[key] = float(source_features[key])
                for prior_slot in SEED_SLOTS:
                    if int(prior_slot) >= int(slot):
                        continue
                    prior_d1 = source_features.get(f"d1_{prior_slot}")
                    prior_d0 = source_features.get(f"d0_{prior_slot}")
                    if number(prior_d1):
                        features[f"d1_prior_{prior_slot}"] = float(prior_d1)
                    if number(prior_d0):
                        features[f"d0_prior_{prior_slot}"] = float(prior_d0)
                hour = int(slot)
                features["issue_hour_sin"] = math.sin(2 * math.pi * hour / 24)
                features["issue_hour_cos"] = math.cos(2 * math.pi * hour / 24)
            if not number(base) or not number(source.get("actual_c")):
                continue
            history.append({
                "issue_date": source["issue_date"],
                "target_date": source.get(
                    "target_date",
                    (dt.date.fromisoformat(source["issue_date"]) + dt.timedelta(days=1)).isoformat(),
                ),
                "slot": slot,
                "base": float(base),
                "actual_c": float(source["actual_c"]),
                "features": features,
            })
    return history


def verify_seed(seed_path=SEED_PATH):
    seed = json.loads(pathlib.Path(seed_path).read_text(encoding="utf-8"))
    history = _seed_history(seed)
    by_slot = {}
    for slot in SEED_SLOTS:
        rows = [row for row in history if row["slot"] == slot]
        baseline_errors = []
        corrected_errors = []
        active_baseline_errors = []
        active_corrected_errors = []
        first_test = last_test = None

        for current in rows:
            correction, train_n = train_predict(history, current)
            baseline = round_noaa(current["base"])
            candidate = corrected_integer(baseline, correction)
            actual = int(current["actual_c"])
            baseline_errors.append(baseline - actual)
            corrected_errors.append(candidate - actual)
            if train_n >= HISTORY_WINDOW and correction is not None:
                active_baseline_errors.append(baseline - actual)
                active_corrected_errors.append(candidate - actual)
                first_test = first_test or current["issue_date"]
                last_test = current["issue_date"]

        by_slot[slot] = {
            "label_days": len(rows),
            "candidate_test_days": len(active_corrected_errors),
            "candidate_test_start": first_test,
            "candidate_test_end": last_test,
            "full_period_baseline": metrics(baseline_errors),
            "full_period_candidate": metrics(corrected_errors),
            "active_window_baseline": metrics(active_baseline_errors),
            "active_window_candidate": metrics(active_corrected_errors),
        }

    return {"source": seed.get("source"), "by_issue_hour": by_slot}


def _probability_features(day, features):
    probabilities = day.get("raw_probabilities") or {}
    probs = {int(key): float(value) for key, value in probabilities.items() if number(value)}
    total = sum(probs.values())
    if total <= 0:
        return
    probs = {key: value / total for key, value in probs.items()}
    mean = sum(key * value for key, value in probs.items())
    features.update({
        "base": float(day["main_c"]),
        "pmean": mean,
        "pvar": sum((key - mean) ** 2 * value for key, value in probs.items()),
        "entropy": -sum(value * math.log(max(value, 1e-12)) for value in probs.values()),
    })
    for quantile in (0.1, 0.25, 0.5, 0.75, 0.9):
        accumulated = 0.0
        for key in sorted(probs):
            accumulated += probs[key]
            if accumulated >= quantile:
                features[f"p{quantile}"] = key
                break


def _family_features(day, features):
    families = day.get("families", [])
    for family in families:
        name = family.get("family")
        members = [member for member in family.get("members", []) if number(member.get("max"))]
        if not name or not members:
            continue
        prefix = "gfs" if name == "NCEP" else str(name).lower()
        maxima = sorted(float(member["max"]) for member in members)
        median_max = float(statistics.median(maxima))
        center = min(members, key=lambda member: abs(float(member["max"]) - median_max))
        features[f"max_{prefix}"] = median_max
        if number(family.get("bandwidth")):
            features[f"bandwidth_{prefix}"] = float(family["bandwidth"])
        try:
            features[f"peak_hour_{prefix}"] = engine.stamp(center["time"]).hour
        except (KeyError, TypeError, ValueError):
            pass
        for key, value in (center.get("features") or {}).items():
            if not number(value):
                continue
            if key == "direction":
                angle = math.radians(float(value))
                features[f"{prefix}_wind_sin"] = math.sin(angle)
                features[f"{prefix}_wind_cos"] = math.cos(angle)
            else:
                features[f"{prefix}_{key}"] = float(value)


def build_live_features(snapshot, result, issue_date, slots, slot):
    day = result["days"][1]
    features = {}
    _probability_features(day, features)
    _family_features(day, features)
    hour = int(slot)
    features["d1_current"] = float(day["main_c"])
    features["issue_hour_sin"] = math.sin(2 * math.pi * hour / 24)
    features["issue_hour_cos"] = math.cos(2 * math.pi * hour / 24)

    d0 = result["days"][0].get("main_c")
    if number(d0):
        features["d0_current"] = float(d0)
    prior_d1 = []
    for prior_slot in FORECAST_SLOTS:
        if int(prior_slot) >= hour:
            continue
        prior = slots.get(prior_slot, {})
        if number(prior.get("d1")):
            value = float(prior["d1"])
            features[f"d1_prior_{prior_slot}"] = value
            prior_d1.append(value)
        if number(prior.get("d0")):
            features[f"d0_prior_{prior_slot}"] = float(prior["d0"])
    if prior_d1:
        features["d1_revision_from_first"] = float(day["main_c"]) - prior_d1[0]
        features["d1_prior_range"] = max(prior_d1 + [float(day["main_c"])]) - min(
            prior_d1 + [float(day["main_c"])]
        )

    observed = []
    issue = dt.date.fromisoformat(issue_date)
    decision = engine.stamp(snapshot.get("decision_time") or snapshot["reference_time"])
    for row in snapshot.get("observations", []):
        if not number(row.get("temp_c")):
            continue
        try:
            stamp = engine.stamp(row["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if stamp.date() == issue and stamp <= decision:
            observed.append((stamp, float(row["temp_c"])))
    observed.sort(key=lambda pair: pair[0])
    if observed:
        values = [value for stamp, value in observed]
        high = max(values)
        high_stamp = next(stamp for stamp, value in observed if value == high)
        features.update({
            "today_high": high,
            "today_min": min(values),
            "today_last": values[-1],
            "today_range": high - min(values),
            "today_high_hour": high_stamp.hour,
        })

    issue_day = dt.date.fromisoformat(issue_date)
    day_of_year = issue_day.timetuple().tm_yday
    features["season_sin"] = math.sin(2 * math.pi * day_of_year / 365.25)
    features["season_cos"] = math.cos(2 * math.pi * day_of_year / 365.25)

    if number(d0) and observed:
        features["obs_minus_d0_current"] = max(value_c for stamp, value_c in observed) - float(d0)

    return features


def _observation_days(snapshot):
    grouped = defaultdict(list)
    for row in snapshot.get("observations", []):
        if not number(row.get("temp_c")):
            continue
        try:
            stamp = engine.stamp(row["time"])
        except (KeyError, TypeError, ValueError):
            continue
        grouped[stamp.date().isoformat()].append((stamp, float(row["temp_c"])))
    return grouped


def _label_completed_rows(state, snapshot):
    days = _observation_days(snapshot)
    for issue_date, row in state.get("live_rows", {}).items():
        if number(row.get("actual_c")):
            continue
        records = days.get(row.get("target_date"), [])
        if len(records) >= 40 and max(stamp.hour * 60 + stamp.minute for stamp, value in records) >= 23 * 60:
            row["actual_c"] = round_noaa(max(value for stamp, value in records))


def _read_json(path, fallback):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


def apply_run(run_dir, seed_path=SEED_PATH, live_path=LIVE_PATH, scheduled_slot=None):
    run_dir = pathlib.Path(run_dir)
    snapshot = json.loads((run_dir / "snapshot.json").read_text(encoding="utf-8"))
    result_path = run_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    issue_date = snapshot["target_day"]
    slot = str(scheduled_slot or "")
    if slot not in FORECAST_SLOTS:
        return {"applied": False, "reason": "outside the scheduled three-hour forecast slots"}
    decision = engine.stamp(snapshot.get("decision_time") or snapshot["reference_time"])
    scheduled_time = decision.replace(hour=int(slot), minute=0, second=0, microsecond=0)
    if abs(decision - scheduled_time) > dt.timedelta(minutes=45):
        return {"applied": False, "reason": "scheduled run started over 45 minutes from its test slot"}

    live = _read_json(live_path, {"schema_version": 1, "slots": {}, "live_rows": {}})
    live.setdefault("slots", {})
    live.setdefault("live_rows", {})
    _label_completed_rows(live, snapshot)
    slots = live["slots"].setdefault(issue_date, {})
    if len(result.get("days", [])) < 2 or result["days"][1].get("status") != "ok":
        return {"applied": False, "reason": "D1 forecast unavailable"}

    slots[slot] = {
        "d0": result["days"][0].get("main_c"),
        "d1": result["days"][1].get("main_c"),
    }
    features = build_live_features(snapshot, result, issue_date, slots, slot)
    current = {
        "issue_date": issue_date,
        "target_date": (dt.date.fromisoformat(issue_date) + dt.timedelta(days=1)).isoformat(),
        "slot": slot,
        "base": float(result["days"][1]["main_c"]),
        "features": features,
    }
    seed = json.loads(pathlib.Path(seed_path).read_text(encoding="utf-8"))
    history = _seed_history(seed)
    history.extend(live["live_rows"].values())
    correction, train_n = train_predict(history, current)
    row_key = f"{issue_date}:{slot}"
    live["live_rows"][row_key] = dict(current, actual_c=None)
    response = {
        "applied": False,
        "recorded": True,
        "slot": slot,
        "issue_date": issue_date,
        "training_rows": train_n,
        "correction_c": correction,
    }
    day = result["days"][1]
    old_main = int(day["main_c"])
    if correction is None:
        reason = f"{slot}:00 için {train_n}/{HISTORY_WINDOW} tamamlanmış saat-eşleşmeli gün var"
        response["reason"] = reason
    else:
        new_main = corrected_integer(old_main, correction)
        day["candidate95"] = {
            "applied": True,
            "source": "hour-specific rolling NOAA residual model",
            "training_slot": slot,
            "training_rows": train_n,
            "raw_main_c": old_main,
            "correction_c": round(correction, 4),
            "adjusted_main_c": new_main,
        }
        day["main_c"] = new_main
        response.update({"applied": True, "changed": new_main != old_main, "raw_main_c": old_main, "adjusted_main_c": new_main})

    if correction is None:
        day["candidate95"] = {
            "applied": False,
            "source": "hour-specific rolling NOAA residual model",
            "training_slot": slot,
            "training_rows": train_n,
            "raw_main_c": old_main,
            "reason": reason,
        }
    result.pop("result_sha256", None)
    result["result_sha256"] = engine.digest(result)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    forecast = engine.render(result)
    if correction is None:
        forecast += f"\n\n> Saatlik NOAA kalibrasyonu: {reason}; bu çalıştırmada nokta düzeltmesi uygulanmadı."
    (run_dir / "forecast.md").write_text(forecast, encoding="utf-8")

    pathlib.Path(live_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(live_path).write_text(json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8")
    return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-seed", action="store_true")
    parser.add_argument("--run-dir", default="ltfm-run")
    parser.add_argument("--seed", default=str(SEED_PATH))
    parser.add_argument("--live", default=str(LIVE_PATH))
    parser.add_argument("--slot", default="")
    args = parser.parse_args()
    if args.verify_seed:
        print(json.dumps(verify_seed(args.seed), ensure_ascii=False, indent=2))
        return
    print(json.dumps(apply_run(args.run_dir, args.seed, args.live, args.slot), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
