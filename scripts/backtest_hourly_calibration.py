#!/usr/bin/env python3
"""Walk-forward test the LTFM NOAA correction at all 3-hour issue slots."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import csv
import datetime as dt
import json
import pathlib
import sys
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_issue_times as archive  # noqa: E402
import ltfm_candidate95 as calibration  # noqa: E402
import ltfm_engine_v2 as engine  # noqa: E402

TZ = ZoneInfo("Europe/Istanbul")
ISSUE_TIMES = ("00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00")


def score(rows: list[dict], forecast_key: str) -> dict:
    valid = [r for r in rows if r.get(forecast_key) is not None and r.get("actual_c") is not None]
    if not valid:
        return {"n": 0, "exact_hits": 0, "exact_accuracy_pct": None, "within_1c": 0,
                "within_1c_pct": None, "mae_c": None, "bias_c": None}
    errors = [int(r[forecast_key]) - int(r["actual_c"]) for r in valid]
    exact = sum(error == 0 for error in errors)
    within = sum(abs(error) <= 1 for error in errors)
    return {
        "n": len(valid),
        "exact_hits": exact,
        "exact_accuracy_pct": round(100 * exact / len(valid), 2),
        "within_1c": within,
        "within_1c_pct": round(100 * within / len(valid), 2),
        "mae_c": round(sum(abs(error) for error in errors) / len(errors), 3),
        "bias_c": round(sum(errors) / len(errors), 3),
    }


def load_noaa_labels(path: pathlib.Path) -> dict[str, int]:
    seed = json.loads(path.read_text(encoding="utf-8"))
    labels = {}
    for row in seed.get("history", []):
        issue = dt.date.fromisoformat(row["issue_date"])
        target = row.get("target_date", (issue + dt.timedelta(days=1)).isoformat())
        if row.get("actual_c") is not None:
            labels[target] = int(row["actual_c"])
    if not labels:
        raise RuntimeError(f"No NOAA target labels found in {path}")
    return labels


def write_csv(path: pathlib.Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def render(summary: dict) -> str:
    lines = [
        "# LTFM — 8 tahmin saatinde NOAA walk-forward backtest",
        "",
        f"- Dönem: {summary['issue_date_start']}–{summary['issue_date_end']} ({summary['issue_days']} issue günü)",
        f"- NOAA etiketli D1 hedef günü: {summary['noaa_label_days']}",
        "- Hedef: issue gününden sonraki yerel günün NOAA maksimum sıcaklığı; etiketler `candidate95_seed.json` içindeki NOAA/NWS LTFM kayıtlarından alındı.",
        "- Tahmin girdileri: her issue saatinden en az 6 saat önceki Open-Meteo Single Runs; canlı issue anı gözlemleri IEM LTFM METAR/SPECI arşivinden.",
        f"- Kalibrasyon: her slot için önceki son {calibration.HISTORY_WINDOW} tamamlanmış aynı-slot günü; ilk 60 gün eğitimde, skor yalnızca sonrasında.",
        "- Düzeltme: mevcut NOAA residual modeli; çıktı en fazla bir tam °C yukarı veya aşağı kaydırılır.",
        "- Bu walk-forward testte tüm tahmin geçmişi aynı arşiv döneminden sırayla üretildi; gelecekteki günler eğitime sızdırılmadı.",
        "",
        "| Saat TRT | NOAA gün | Test N | Taban tam | Düzeltilmiş tam | Taban ±1°C | Düzeltilmiş ±1°C | Taban MAE | Düzeltilmiş MAE |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for slot in calibration.FORECAST_SLOTS:
        item = summary["by_slot"][slot]
        base, candidate = item["active_base"], item["active_candidate"]
        pct = lambda x, k: "—" if x[k] is None else f"{x[k]:.2f}%"
        mae = lambda x: "—" if x["mae_c"] is None else f"{x['mae_c']:.3f}°C"
        lines.append(
            f"| {slot}:00 | {item['noaa_label_days']} | {candidate['n']} | "
            f"{base['exact_hits']}/{base['n']} ({pct(base, 'exact_accuracy_pct')}) | "
            f"{candidate['exact_hits']}/{candidate['n']} ({pct(candidate, 'exact_accuracy_pct')}) | "
            f"{pct(base, 'within_1c_pct')} | {pct(candidate, 'within_1c_pct')} | {mae(base)} | {mae(candidate)} |"
        )
    lines += [
        "",
        "## Tamlık ve yorum",
        "",
        "Taban ve düzeltilmiş sonuçlar aynı aktif test günlerinde karşılaştırılmıştır. `predictions.csv` her günün taban tahmini, düzeltilmiş tahmini, eğitim örneği sayısı, düzeltme ve NOAA etiketi içerir. `hourly_seed.json` canlı motora saat-eşleşmeli geçmiş olarak aktarılabilecek feature ve NOAA etiketlerini içerir.",
        "",
        f"Tahmin üretilemeyen issue-slot sayısı: {summary['unavailable_forecasts']}. NOAA etiketi olmayan D1 gün sayısı/slot: {summary['missing_noaa_labels']}.",
        "",
        "## Önemli kaynak ayrımı",
        "",
        "NOAA/NWS seed dosyası yalnızca gerçekleşen hedefleri sağlar. Geçmiş tahmin girdileri yeniden Open-Meteo arşivinden üretilmiştir; issue anındaki geçmiş yüzey gözlemleri IEM METAR/SPECI arşivinden alınmıştır. Böylece 8 tahmin saati aynı motor, aynı issue-time cutoff ve aynı NOAA hedef etiketleriyle karşılaştırılmıştır.",
        "",
    ]
    return "\n".join(lines)


def run(args) -> dict:
    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date)
    if end < start:
        raise SystemExit("end-date must be on or after start-date")
    issue_days = list(archive.daterange(start, end))
    if len(issue_days) < 170:
        raise SystemExit(f"Need at least 170 issue dates, received {len(issue_days)}")

    labels = load_noaa_labels(pathlib.Path(args.noaa_seed))
    actual_end = end + dt.timedelta(days=1)
    observations, raw_obs = archive.fetch_observations(start - dt.timedelta(days=1), actual_end)
    by_day, _ = archive.make_day_index(observations)

    tasks = [(day, issue_time) for day in issue_days for issue_time in ISSUE_TIMES]
    # Several issue times share the same latest six-hour model cycle. Fetch
    # each unique cycle once, then reuse its immutable run data at each issue
    # time while rebuilding the issue-time observations and metadata.
    cycle_representatives = {}
    for day, issue_time in tasks:
        cycle = archive.latest_cycle(day, issue_time)
        cycle_representatives.setdefault(cycle, (day, issue_time))
    cycle_results = {}
    snapshots = {}
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(archive.fetch_issue_models, day, issue_time, args.max_fallback_cycles): cycle
            for cycle, (day, issue_time) in cycle_representatives.items()
        }
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            cycle = futures[future]
            try:
                cycle_results[cycle] = future.result()
            except Exception as exc:
                errors.append({"forecast_cycle_utc": cycle.isoformat(), "error": str(exc)})
            if index % 40 == 0 or index == len(futures):
                print(f"Fetched {index}/{len(futures)} unique forecast cycles; failed={len(errors)}", flush=True)

    for day, issue_time in tasks:
        cycle = archive.latest_cycle(day, issue_time)
        if cycle not in cycle_results:
            continue
        decision = dt.datetime.combine(day, dt.time.fromisoformat(issue_time), TZ).astimezone(dt.timezone.utc)
        model_results = copy.deepcopy(cycle_results[cycle])
        for item in model_results:
            run_time = archive.parse_stamp(item["run_time"])
            item["run_age_hours"] = round((decision - run_time).total_seconds() / 3600, 2)
        try:
            snapshot = archive.issue_snapshot(day, issue_time, model_results, by_day, actual_end)
            snapshot["backtest_metadata"]["target_actual_source"] = "NOAA/NWS LTFM labels from data/candidate95_seed.json"
            snapshots[(day.isoformat(), issue_time)] = (snapshot, model_results)
        except Exception as exc:
            errors.append({"issue_date": day.isoformat(), "issue_time": issue_time, "error": str(exc)})

    out = pathlib.Path(args.out)
    snapshot_dir = out / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (out / "issue_observations.csv").write_bytes(raw_obs)
    prediction_rows = []
    manifest = []
    history = []
    hourly_seed = []

    for day in issue_days:
        day_key = day.isoformat()
        prior_slot_forecasts = {}
        for issue_time in ISSUE_TIMES:
            slot = issue_time[:2]
            pair = snapshots.get((day_key, issue_time))
            target = (day + dt.timedelta(days=1)).isoformat()
            actual = labels.get(target)
            if pair is None:
                prediction_rows.append({
                    "issue_date": day_key, "issue_time_local": issue_time, "slot": slot,
                    "target_date": target, "base_forecast_c": None, "candidate_forecast_c": None,
                    "actual_c": actual, "training_rows": 0, "correction_c": None,
                    "trained": False, "status": "input_unavailable",
                })
                continue

            snapshot, model_results = pair
            archive.write_gzip_json(snapshot_dir / f"{day_key}_{slot}00.json.gz", snapshot)
            runs = snapshot["backtest_metadata"]["selected_model_runs"]
            for model, run_info in sorted(runs.items()):
                manifest.append({"issue_date": day_key, "issue_time_local": issue_time, "model": model, **run_info})

            result = engine.execute(snapshot)
            d0, d1 = result["days"]
            if d1.get("status") != "ok":
                prediction_rows.append({
                    "issue_date": day_key, "issue_time_local": issue_time, "slot": slot,
                    "target_date": target, "base_forecast_c": None, "candidate_forecast_c": None,
                    "actual_c": actual, "training_rows": 0, "correction_c": None,
                    "trained": False, "status": d1.get("status", "engine_error"),
                })
                continue

            # As in production, later issue-hour features see earlier base forecasts from this local date.
            prior_slot_forecasts[slot] = {"d0": d0.get("main_c"), "d1": d1.get("main_c")}
            features = calibration.build_live_features(snapshot, result, day_key, prior_slot_forecasts, slot)
            current = {
                "issue_date": day_key,
                "target_date": target,
                "slot": slot,
                "base": float(d1["main_c"]),
                "features": features,
            }
            correction, train_n = calibration.train_predict(history, current)
            trained = correction is not None
            base = calibration.round_noaa(current["base"])
            candidate = calibration.corrected_integer(base, correction) if trained else base
            prediction_rows.append({
                "issue_date": day_key,
                "issue_time_local": issue_time,
                "slot": slot,
                "target_date": target,
                "base_forecast_c": base,
                "candidate_forecast_c": candidate,
                "actual_c": actual,
                "training_rows": train_n,
                "correction_c": round(correction, 4) if trained else None,
                "trained": trained,
                "status": "ok" if actual is not None else "no_noaa_label",
                "input_sha256": engine.digest(snapshot),
                "selected_model_runs": {k: v["run_time"] for k, v in sorted(runs.items())},
            })
            if actual is not None:
                row = dict(current, actual_c=int(actual))
                history.append(row)
                hourly_seed.append(row)

    by_slot = {}
    for slot in calibration.FORECAST_SLOTS:
        rows = [r for r in prediction_rows if r["slot"] == slot]
        actual_rows = [r for r in rows if r.get("actual_c") is not None and r.get("base_forecast_c") is not None]
        active = [r for r in actual_rows if r["trained"]]
        by_slot[slot] = {
            "forecast_rows": sum(r.get("base_forecast_c") is not None for r in rows),
            "noaa_label_days": sum(r.get("actual_c") is not None for r in rows),
            "training_only_days": sum(r.get("actual_c") is not None and not r["trained"] for r in rows),
            "active_base": score(active, "base_forecast_c"),
            "active_candidate": score(active, "candidate_forecast_c"),
            "all_period_base": score(actual_rows, "base_forecast_c"),
            "first_active_date": next((r["issue_date"] for r in active), None),
            "last_active_date": next((r["issue_date"] for r in reversed(active)), None),
        }

    summary = {
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "engine_version": engine.VERSION,
        "issue_date_start": start.isoformat(),
        "issue_date_end": end.isoformat(),
        "issue_days": len(issue_days),
        "decision_times_local": list(ISSUE_TIMES),
        "noaa_label_days": len(labels),
        "missing_noaa_labels": sum(1 for day in issue_days if (day + dt.timedelta(days=1)).isoformat() not in labels),
        "noaa_seed_sha256": archive.sha256_bytes(pathlib.Path(args.noaa_seed).read_bytes()),
        "issue_observation_archive_sha256": archive.sha256_bytes(raw_obs),
        "forecast_data_source": "Open-Meteo Single Runs API; archived issue run cutoff = local issue time minus six hours",
        "issue_observation_source": "IEM LTFM METAR/SPECI archive; issue-time inputs only",
        "target_actual_source": "NOAA/NWS LTFM labels in candidate95_seed.json",
        "warmup_days": calibration.HISTORY_WINDOW,
        "errors": errors,
        "unavailable_forecasts": sum(1 for row in prediction_rows if row.get("base_forecast_c") is None),
        "prediction_rows": len(prediction_rows),
        "hourly_history_rows": len(hourly_seed),
        "by_slot": by_slot,
    }
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "predictions.csv", prediction_rows)
    write_csv(out / "run_manifest.csv", manifest)
    (out / "hourly_seed.json").write_text(json.dumps({
        "schema_version": 1,
        "source": "Walk-forward archived forecasts with NOAA/NWS LTFM daily maximum labels",
        "issue_date_start": start.isoformat(),
        "issue_date_end": end.isoformat(),
        "history": hourly_seed,
    }, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(render(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default="2026-04-02")
    parser.add_argument("--end-date", default="2026-09-22")
    parser.add_argument("--noaa-seed", default=str(ROOT / "data" / "candidate95_seed.json"))
    parser.add_argument("--out", default="hourly-backtest-output")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-fallback-cycles", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be between 1 and 16")
    run(args)


if __name__ == "__main__":
    main()
