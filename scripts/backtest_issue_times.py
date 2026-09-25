#!/usr/bin/env python3
"""Replay the locked LTFM engine at fixed issue times against archived runs.

Uses exact Open-Meteo Single Runs, conservatively limited to cycles at least
six hours old at issue time, plus IEM LTFM routine and special METAR actuals.
The engine's deterministic fallback is used when historical ICON EPS members
are not available. No model weights are fitted on the scoring dates.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ltfm_engine_v2 as engine  # noqa: E402

TZ = ZoneInfo("Europe/Istanbul")
UTC = dt.timezone.utc
LAT = 41.27528
LON = 28.75194
ELEVATION = 99.0
SINGLE_RUNS = "https://single-runs-api.open-meteo.com/v1/forecast"
IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
ISSUE_TIMES = ("09:00", "15:00", "22:00")
MODEL_IDS = {
    "ecmwf_ifs": "ecmwf_ifs",
    "icon_eu": "icon_eu",
    "ncep_gfs_seamless": "gfs_seamless",
}
HOURLY = [
    "temperature_2m", "dew_point_2m", "relative_humidity_2m",
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "precipitation", "shortwave_radiation", "wind_speed_10m",
    "wind_direction_10m", "wind_gusts_10m", "pressure_msl", "surface_pressure",
    "temperature_925hPa", "temperature_850hPa", "relative_humidity_925hPa",
    "relative_humidity_850hPa", "wind_direction_925hPa", "wind_direction_850hPa",
    "geopotential_height_925hPa",
]
CORE_HOURLY = HOURLY[:14]
HORIZONS = ((0, "D0"), (1, "D1"), (2, "D2"))


class DataError(RuntimeError):
    pass


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_stamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def rounded_celsius(value: float) -> int:
    # Match NOAA's metric-table Math.round(x): floor(x + 0.5), including negatives.
    return math.floor(value + 0.5)


def daterange(start: dt.date, end: dt.date):
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def request_bytes(url: str, timeout: int = 90, attempts: int = 5) -> bytes:
    headers = {"User-Agent": "LTFM-174-day-issue-time-backtest/1.0"}
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:800]
            detail = f"HTTP {exc.code}: {body}"
            last = DataError(detail)
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except Exception as exc:  # transient DNS, socket, or timeout failures
            last = exc
        if attempt + 1 < attempts:
            time.sleep(min(2 ** attempt, 16))
    raise DataError(str(last))


def fetch_json(endpoint: str, params: list[tuple[str, str]]) -> dict:
    url = endpoint + "?" + urllib.parse.urlencode(params)
    raw = request_bytes(url)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise DataError(f"API returned invalid JSON: {exc}") from exc
    if payload.get("error"):
        raise DataError(str(payload.get("reason", "API error")))
    if not isinstance(payload.get("hourly"), dict) or not payload["hourly"].get("time"):
        raise DataError("API response has no hourly data")
    return payload


def latest_cycle(issue_day: dt.date, issue_time: str) -> dt.datetime:
    local = dt.datetime.combine(issue_day, dt.time.fromisoformat(issue_time), TZ)
    # Open-Meteo documents a typical 4–6 h distribution delay for global runs;
    # six hours is used as the conservative availability cutoff.
    cutoff = local.astimezone(UTC) - dt.timedelta(hours=6)
    hour = (cutoff.hour // 6) * 6
    return cutoff.replace(hour=hour, minute=0, second=0, microsecond=0)


def model_params(model_id: str, run: dt.datetime, variables: list[str]) -> list[tuple[str, str]]:
    pairs = [
        ("latitude", str(LAT)), ("longitude", str(LON)),
        ("elevation", str(ELEVATION)), ("timezone", "Europe/Istanbul"),
        ("past_hours", "12"), ("forecast_days", "5"),
        ("hourly", ",".join(variables)),
        ("temperature_unit", "celsius"), ("wind_speed_unit", "kmh"),
        ("precipitation_unit", "mm"), ("cell_selection", "land"),
        ("models", model_id), ("run", run.strftime("%Y-%m-%dT%H:%M")),
    ]
    return pairs


def fetch_model(model_key: str, issue_day: dt.date, issue_time: str, max_fallback_cycles: int):
    model_id = MODEL_IDS[model_key]
    first_cycle = latest_cycle(issue_day, issue_time)
    errors: list[str] = []
    for fallback in range(max_fallback_cycles + 1):
        run = first_cycle - dt.timedelta(hours=6 * fallback)
        for variables, surface_only in ((HOURLY, False), (CORE_HOURLY, True)):
            try:
                data = fetch_json(SINGLE_RUNS, model_params(model_id, run, variables))
                # The archive query identifies its own valid cycle; preserve it so
                # the engine's look-ahead guard can independently verify cutoff.
                data["run_time"] = run.isoformat()
                data["source_model"] = model_id
                return {
                    "key": model_key,
                    "data": data,
                    "run_time": run.isoformat(),
                    "run_age_hours": round((dt.datetime.combine(issue_day, dt.time.fromisoformat(issue_time), TZ).astimezone(UTC) - run).total_seconds() / 3600, 2),
                    "surface_only": surface_only,
                    "fallback_cycles": fallback,
                    "response_sha256": sha256_bytes(canonical(data).encode("utf-8")),
                    "warnings": errors,
                }
            except Exception as exc:
                errors.append(f"{run.isoformat()} {'core' if surface_only else 'full'}: {exc}")
    raise DataError(f"{model_key} has no usable run at {issue_day} {issue_time}; " + " | ".join(errors[-4:]))


def fetch_observations(start: dt.date, end: dt.date) -> tuple[list[dict], bytes]:
    # IEM range end is exclusive. Include all reports through end-date 23:59 UTC.
    end_exclusive = end + dt.timedelta(days=1)
    params = [
        ("station", "LTFM"), ("data", "tmpc"),
        ("year1", str(start.year)), ("month1", str(start.month)), ("day1", str(start.day)),
        ("year2", str(end_exclusive.year)), ("month2", str(end_exclusive.month)), ("day2", str(end_exclusive.day)),
        ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"), ("elev", "no"),
        ("missing", "empty"), ("trace", "T"), ("direct", "no"),
        # IEM report_type 3 and 4 mean routine METAR and SPECI.
        ("report_type", "3"), ("report_type", "4"),
    ]
    raw = request_bytes(IEM_ASOS + "?" + urllib.parse.urlencode(params))
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"station", "valid", "tmpc"}.issubset(set(reader.fieldnames)):
        raise DataError("IEM response did not contain station, valid, tmpc columns")
    rows = []
    for row in reader:
        if (row.get("station") or "").strip().upper() != "LTFM":
            continue
        raw_temp = (row.get("tmpc") or "").strip()
        if not raw_temp or raw_temp.upper() in {"M", "NA", "NULL"}:
            continue
        try:
            temp = float(raw_temp)
            when = parse_stamp((row.get("valid") or "").strip())
        except (TypeError, ValueError):
            continue
        if not math.isfinite(temp) or not -90 <= temp <= 65:
            continue
        rows.append({"station": "LTFM", "time": when.isoformat(), "temp_c": temp, "valid": True})
    if not rows:
        raise DataError("IEM returned no usable LTFM temperature observations")
    return rows, raw


def make_day_index(observations: list[dict]):
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in observations:
        local = parse_stamp(row["time"]).astimezone(TZ)
        by_day[local.date().isoformat()].append(row)
    actuals = {}
    for day, rows in by_day.items():
        actuals[day] = max(rounded_celsius(float(row["temp_c"])) for row in rows)
    return by_day, actuals


def issue_snapshot(issue_day: dt.date, issue_time: str, model_results: list[dict], by_day: dict, actual_end: dt.date):
    decision = dt.datetime.combine(issue_day, dt.time.fromisoformat(issue_time), TZ)
    model_cutoff = decision.astimezone(UTC) - dt.timedelta(hours=6)
    det = {}
    runs = {}
    warnings = []
    for item in model_results:
        data = item["data"]
        # Omit rather than leak a future run, even if a source error returned one.
        run_time = parse_stamp(item["run_time"])
        if run_time > model_cutoff:
            warnings.append(f"{item['key']} run later than conservative cutoff was rejected")
            continue
        det[item["key"]] = data
        runs[item["key"]] = {k: item[k] for k in ("run_time", "run_age_hours", "surface_only", "fallback_cycles", "response_sha256")}
        warnings.extend(item.get("warnings", []))
    # The engine only needs this day's observations for the D0 floor and the
    # preceding six-hour residual window; at all three issue times that window
    # remains within the same LTFM local calendar day.
    obs = []
    for row in by_day.get(issue_day.isoformat(), []):
        if parse_stamp(row["time"]) <= decision.astimezone(UTC):
            obs.append(row)
    snapshot = {
        "reference_time": decision.isoformat(),
        "decision_time": decision.isoformat(),
        "target_day": issue_day.isoformat(),
        "deterministic": det,
        "ensemble": {},
        "observations": obs,
        "warnings": warnings,
        "source_hashes": {"model_runs": sha256_bytes(canonical(runs).encode("utf-8"))},
        "backtest_metadata": {
            "engine_version": engine.VERSION,
            "issue_time_local": issue_time,
            "run_cutoff_utc": model_cutoff.isoformat(),
            "selected_model_runs": runs,
            "observation_source": "IEM LTFM ASOS/METAR archive, routine + SPECI, tmpc",
            "target_actual_source": "IEM LTFM ASOS/METAR archive, routine + SPECI, tmpc rounded as NOAA Math.round",
            "target_data_end": actual_end.isoformat(),
            "historical_icon_eps_available": False,
        },
    }
    return snapshot


def write_gzip_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as stream:
        stream.write(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def score_rows(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["forecast_c"] is not None and r["observed_c"] is not None]
    if not scored:
        return {"n": 0, "exact_hits": 0, "exact_accuracy_pct": None, "within_1c": 0, "within_1c_pct": None, "mae_c": None, "bias_c": None}
    errors = [r["forecast_c"] - r["observed_c"] for r in scored]
    exact = sum(e == 0 for e in errors)
    within = sum(abs(e) <= 1 for e in errors)
    return {
        "n": len(scored),
        "exact_hits": exact,
        "exact_accuracy_pct": round(100 * exact / len(scored), 2),
        "within_1c": within,
        "within_1c_pct": round(100 * within / len(scored), 2),
        "mae_c": round(statistics.mean(abs(e) for e in errors), 3),
        "bias_c": round(statistics.mean(errors), 3),
    }


def render_report(summary: dict) -> str:
    lines = [
        "# LTFM 174 günlük issue-time backtest",
        "",
        f"- Test zamanı: {summary['created_at']} UTC",
        f"- Issue günleri: {summary['issue_date_start']}–{summary['issue_date_end']} ({summary['issue_days']} gün)",
        "- Karar saatleri: 09:00, 15:00, 22:00 Europe/Istanbul",
        "- Hedefler: D0 aynı gün, D1 ertesi gün, D2 iki gün sonrası; her günün günlük maksimumı",
        f"- Motor: `{summary['engine_version']}`; test kümesinde ağırlık/kalibrasyon öğrenilmedi",
        "- Hava tahmini girdileri: Open-Meteo Single Runs arşivindeki belirli model koşuları; her koşu karar saatinden en az 6 saat önce başlatılmıştır. Bu, arşiv belgesindeki küresel modeller için tipik 4–6 saatlik dağıtım aralığının temkinli 6 saat sınırını kullanır.",
        "- Gözlem ve hedef: IEM LTFM `tmpc` arşivi; routine METAR + SPECI (report type 3 + 4). Tam dereceye dönüşüm `floor(C + 0.5)` ile yapıldı.",
        "- Kapsam notu: IEM gerçek LTFM METAR/SPECI verisidir, ancak 174 günlük NOAA/NWS Time Series `Temp` tablosunun birebir arşivi değildir. Canlı motorun NOAA gözlem beslemesi yerine bu tarihsel arşiv kullanıldı. ICON EPS geçmiş ensemble üyeleri bulunmadığı için motorun deterministik ICON-EU geri dönüşü çalıştı.",
        "",
        "| Hedef | Tahmin saati (TRT) | Tam derece | İsabet | N | ±1°C | MAE | Bias |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon, _ in HORIZONS:
        for issue_time in ISSUE_TIMES:
            key = f"D{horizon}_{issue_time}"
            m = summary["metrics"][key]
            acc = "—" if m["exact_accuracy_pct"] is None else f"{m['exact_accuracy_pct']:.2f}%"
            within = "—" if m["within_1c_pct"] is None else f"{m['within_1c_pct']:.2f}%"
            mae = "—" if m["mae_c"] is None else f"{m['mae_c']:.3f}°C"
            bias = "—" if m["bias_c"] is None else f"{m['bias_c']:+.3f}°C"
            lines.append(f"| D{horizon} | {issue_time} | {acc} | {m['exact_hits']}/{m['n']} | {m['n']} | {within} | {mae} | {bias} |")
    lines += [
        "",
        "## Koşu seçimi",
        "",
        "| Tahmin saati | En son izin verilen global run (UTC) | Gerekçe |",
        "|---|---:|---|",
        "| 09:00 TRT | 00Z | 06Z karar zamanı eksi 6 saat dağıtım payı |",
        "| 15:00 TRT | 06Z | 12Z karar zamanı eksi 6 saat dağıtım payı |",
        "| 22:00 TRT | 12Z | 19Z karar zamanı eksi 6 saat dağıtım payı; 6 saatlik çevrim aşağı yuvarlandı |",
        "",
        "Run bulunamazsa aynı modelin bir önceki 6 saatlik koşusu denendi; seçilen run her tahmin satırının giriş manifestinde tutuldu. D0/D1/D2 puanları aynı 174 issue günü üzerinden eşleştirildi.",
        "",
        "## Kaynaklar",
        "",
        "- Open-Meteo Single Runs: https://open-meteo.com/en/docs/single-runs-api",
        "- IEM ASOS/METAR archive: https://mesonet.agron.iastate.edu/request/download.phtml",
        "",
    ]
    return "\n".join(lines)


def run(args) -> dict:
    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date)
    if end < start:
        raise SystemExit("end-date must be on or after start-date")
    issue_days = list(daterange(start, end))
    if len(issue_days) < 170:
        raise SystemExit(f"Need at least 170 issue dates, received {len(issue_days)}")
    actual_end = end + dt.timedelta(days=2)
    # Six-hour live correction observations on the first decision day begin at
    # local 03:00; request the entire previous local day for margin.
    obs_start = start - dt.timedelta(days=1)
    observations, raw_obs = fetch_observations(obs_start, actual_end)
    by_day, actuals = make_day_index(observations)

    tasks = [(day, issue_time) for day in issue_days for issue_time in ISSUE_TIMES]
    snapshots: dict[tuple[str, str], tuple[dict, dict]] = {}
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_map = {
            pool.submit(fetch_issue_models, day, issue_time, args.max_fallback_cycles): (day, issue_time)
            for day, issue_time in tasks
        }
        completed = 0
        for future in concurrent.futures.as_completed(future_map):
            day, issue_time = future_map[future]
            try:
                model_results = future.result()
                snapshot = issue_snapshot(day, issue_time, model_results, by_day, actual_end)
                snapshots[(day.isoformat(), issue_time)] = (snapshot, {m["key"]: m for m in model_results})
            except Exception as exc:
                errors.append({"issue_date": day.isoformat(), "issue_time": issue_time, "error": str(exc)})
            completed += 1
            if completed % 15 == 0 or completed == len(tasks):
                print(f"Fetched {completed}/{len(tasks)} issue snapshots; failed={len(errors)}", flush=True)

    out = pathlib.Path(args.out)
    input_dir = out / "snapshots"
    input_dir.mkdir(parents=True, exist_ok=True)
    (out / "observations.csv").write_bytes(raw_obs)
    (out / "observations.sha256").write_text(sha256_bytes(raw_obs) + "  observations.csv\n", encoding="utf-8")
    prediction_rows = []
    manifest = []
    for day in issue_days:
        for issue_time in ISSUE_TIMES:
            pair = snapshots.get((day.isoformat(), issue_time))
            if not pair:
                for offset, label in HORIZONS:
                    target = day + dt.timedelta(days=offset)
                    prediction_rows.append({
                        "issue_date": day.isoformat(), "issue_time_local": issue_time,
                        "horizon": label, "target_date": target.isoformat(),
                        "forecast_c": None, "observed_c": actuals.get(target.isoformat()),
                        "status": "input_unavailable", "engine_version": engine.VERSION,
                        "exact_hit": None, "within_1c": None, "absolute_error_c": None,
                    })
                continue
            snapshot, model_results = pair
            frozen = input_dir / f"{day.isoformat()}_{issue_time.replace(':', '')}.json.gz"
            write_gzip_json(frozen, snapshot)
            input_hash = engine.digest(snapshot)
            selected = snapshot["backtest_metadata"]["selected_model_runs"]
            for model, run_info in sorted(selected.items()):
                manifest.append({
                    "issue_date": day.isoformat(), "issue_time_local": issue_time,
                    "model": model, **run_info,
                })
            for offset, label in HORIZONS:
                target = day + dt.timedelta(days=offset)
                observed = actuals.get(target.isoformat())
                try:
                    result = engine.evaluate(snapshot, offset)
                except Exception as exc:
                    result = {"status": "engine_error", "message": str(exc)}
                forecast = result.get("main_c") if result.get("status") == "ok" else None
                error = (forecast - observed) if forecast is not None and observed is not None else None
                prediction_rows.append({
                    "issue_date": day.isoformat(), "issue_time_local": issue_time,
                    "horizon": label, "target_date": target.isoformat(),
                    "forecast_c": forecast, "observed_c": observed,
                    "status": result.get("status", "unknown"),
                    "exact_hit": (error == 0) if error is not None else None,
                    "within_1c": (abs(error) <= 1) if error is not None else None,
                    "absolute_error_c": abs(error) if error is not None else None,
                    "error_c": error,
                    "engine_version": engine.VERSION,
                    "input_sha256": input_hash,
                    "selected_model_runs": {k: v["run_time"] for k, v in sorted(selected.items())},
                    "used_families": ",".join(sorted(f["family"] for f in result.get("families", []))),
                    "warnings": " | ".join(result.get("warnings", [])),
                    "engine_message": result.get("message"),
                })

    keys = [f"D{offset}_{issue_time}" for offset, _ in HORIZONS for issue_time in ISSUE_TIMES]
    metrics = {}
    for key in keys:
        horizon, issue_time = key.split("_")
        rows = [r for r in prediction_rows if r["horizon"] == horizon and r["issue_time_local"] == issue_time]
        metrics[key] = score_rows(rows)
    summary = {
        "created_at": dt.datetime.now(UTC).replace(microsecond=0).isoformat(),
        "engine_version": engine.VERSION,
        "issue_date_start": start.isoformat(), "issue_date_end": end.isoformat(),
        "issue_days": len(issue_days),
        "target_date_end": actual_end.isoformat(),
        "decision_times_local": list(ISSUE_TIMES),
        "horizons": {"D0": "same issue date", "D1": "issue date + 1 local day", "D2": "issue date + 2 local days"},
        "exact_degree_rule": "max(floor(tmpc + 0.5)) across LTFM routine METAR + SPECI reports in each Europe/Istanbul date",
        "forecast_data_source": "Open-Meteo Single Runs API; ecmwf_ifs, icon_eu, gfs_seamless",
        "observation_data_source": "IEM LTFM ASOS/METAR archive; routine and special report types 3 and 4",
        "observation_archive_sha256": sha256_bytes(raw_obs),
        "forecast_run_selection": "latest 6-hour cycle no later than issue time UTC minus 6 hours; fallback by 6-hour steps if unavailable",
        "ensemble_note": "Historical ICON EPS members were unavailable for the full period; the locked engine used its deterministic ICON-EU fallback.",
        "errors": errors,
        "metrics": metrics,
        "rows": len(prediction_rows),
        "scored_rows": sum(1 for r in prediction_rows if r["forecast_c"] is not None and r["observed_c"] is not None),
        "unavailable_forecasts": sum(1 for r in prediction_rows if r["forecast_c"] is None),
        "unavailable_targets": sum(1 for r in prediction_rows if r["observed_c"] is None),
    }
    (out / "predictions.json").write_text(json.dumps(prediction_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if prediction_rows:
        with (out / "predictions.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(prediction_rows[0].keys()))
            writer.writeheader()
            writer.writerows(prediction_rows)
    if manifest:
        with (out / "run_manifest.csv").open("w", newline="", encoding="utf-8") as stream:
            fieldnames = ["issue_date", "issue_time_local", "model", "run_time", "run_age_hours", "surface_only", "fallback_cycles", "response_sha256"]
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(manifest)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(render_report(summary), encoding="utf-8")
    print(json.dumps({"summary": summary, "output_dir": str(out)}, ensure_ascii=False, indent=2))
    return summary


def fetch_issue_models(day: dt.date, issue_time: str, max_fallback_cycles: int):
    return [fetch_model(model, day, issue_time, max_fallback_cycles) for model in MODEL_IDS]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default="2026-04-02", help="first local forecast issue date")
    parser.add_argument("--end-date", default="2026-09-22", help="last local forecast issue date")
    parser.add_argument("--out", default="backtest-output")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-fallback-cycles", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be between 1 and 16")
    run(args)


if __name__ == "__main__":
    main()
