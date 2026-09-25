#!/usr/bin/env python3
"""Run the locked engine against the fresh local data snapshot."""

from __future__ import annotations

import argparse
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ltfm_engine_v2 as engine  # noqa: E402


original_fetch = engine.fetch


def local_or_remote_fetch(url: str) -> bytes:
    if url.endswith("/data/deterministic.json"):
        return (ROOT / "data" / "deterministic.json").read_bytes()
    if url.endswith("/data/ensemble.json"):
        return (ROOT / "data" / "ensemble.json").read_bytes()
    return original_fetch(url)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="ltfm-run")
    args = parser.parse_args()
    engine.fetch = local_or_remote_fetch
    sys.argv = ["ltfm_engine_v2.py", "--fetch", "--out", args.out]
    engine.main()


if __name__ == "__main__":
    main()
