#!/usr/bin/env python3
"""Fetch the live LTFM model inputs used by the locked engine.

The files are intentionally generated in the Actions workspace instead of
committed on every run. The prediction artifact keeps the exact snapshot.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import urllib.parse
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TZ = dt.timezone(dt.timedelta(hours=3))
LAT = 41.27528
LON = 28.75194
ELEVATION = 99.0

SURFACE_HOURLY = [
    "temperature_2m",
    "dew_point_2m",
    "relative_humidity_2m",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "precipitation",
    "shortwave_radiation",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "pressure_msl",
    "surface_pressure",
    "temperature_925hPa",
    "temperature_850hPa",
    "relative_humidity_925hPa",
    "relative_humidity_850hPa",
    "wind_direction_925hPa",
    "wind_direction_850hPa",
    "geopotential_height_925hPa",
]
CORE_HOURLY = SURFACE_HOURLY[:14]

DETERMINISTIC_MODELS = {
    "ecmwf_ifs": "ecmwf_ifs",
    "icon_eu": "icon_eu",
    "ncep_gfs_seamless": "gfs_seamless",
}


def now() -> dt.datetime:
    return dt.datetime.now(TZ).replace(microsecond=0)


def fetch_json(endpoint: str, params: dict[str, object]) -> dict:
    url = endpoint + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "LTFM-GitHub-Actions/1.0"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("error"):
        raise RuntimeError(payload.get("reason", "Open-Meteo returned an error"))
    if not isinstance(payload.get("hourly"), dict):
        raise RuntimeError("Open-Meteo response has no hourly data")
    if not payload["hourly"].get("time"):
        raise RuntimeError("Open-Meteo response has no hourly timestamps")
    return payload


def common_params() -> dict[str, object]:
    return {
        "latitude": LAT,
        "longitude": LON,
        "elevation": ELEVATION,
        "timezone": "Europe/Istanbul",
        "past_hours": 12,
        "forecast_days": 3,
        "hourly": ",".join(SURFACE_HOURLY),
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
        "cell_selection": "land",
    }


def normalize(payload: dict, source_model: str) -> dict:
    result = dict(payload)
    # Keep the provider's selected grid coordinates and elevation intact: the
    # engine uses them to reject mismatched cells. Do not label download time
    # as the upstream model initialization time.
    result["source_model"] = source_model
    return result


def fetch_model(endpoint: str, model: str, errors: list[str]) -> dict:
    params = common_params()
    params["models"] = model
    try:
        return fetch_json(endpoint, params)
    except Exception as detailed_error:
        # Some providers expose fewer pressure-level fields than others.
        # Retry with the shared surface fields so one optional field does not
        # discard an otherwise usable model family.
        params["hourly"] = ",".join(CORE_HOURLY)
        payload = fetch_json(endpoint, params)
        errors.append(
            f"{model}: upper-level fields unavailable; surface-only retry used "
            f"({type(detailed_error).__name__})"
        )
        return payload


def atomic_write(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    deterministic: dict[str, dict] = {}
    errors: list[str] = []
    for engine_name, api_model in DETERMINISTIC_MODELS.items():
        try:
            deterministic[engine_name] = normalize(
                fetch_model("https://api.open-meteo.com/v1/forecast", api_model, errors),
                api_model,
            )
        except Exception as exc:  # Keep another model usable if one endpoint fails.
            errors.append(f"{engine_name}: {type(exc).__name__}: {exc}")

    if len(deterministic) < 2:
        raise SystemExit(
            "Fewer than two deterministic model families were fetched: "
            + " | ".join(errors)
        )

    ensemble: dict[str, dict] = {}
    try:
        ensemble["dwd_icon_eu_eps"] = normalize(
            fetch_model(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                "icon_seamless_eps",
                errors,
            ),
            "icon_seamless_eps",
        )
    except Exception as exc:
        errors.append(f"dwd_icon_eu_eps: {type(exc).__name__}: {exc}")

    atomic_write(DATA_DIR / "deterministic.json", deterministic)
    atomic_write(DATA_DIR / "ensemble.json", ensemble)
    atomic_write(
        DATA_DIR / "refresh_metadata.json",
        {"refreshed_at": now().isoformat(), "errors": errors},
    )
    print(
        json.dumps(
            {
                "deterministic_models": sorted(deterministic),
                "ensemble_models": sorted(ensemble),
                "warnings": errors,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
