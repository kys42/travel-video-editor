#!/usr/bin/env python3
"""Prepare every verified proxy for later model review.

This runner intentionally stops at deterministic media processing: original/proxy
lineage, Phase 1 scene evidence, and Apple dual-locale STT.  Model-authored scene
reviews, transcript reconciliation, summaries, and HTML rendering are separate
packet-review stages.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


COMPLETE_PROXY_EVENTS = {"completed", "reused"}
VIDEO_SUFFIXES = (".MP4", ".mp4", ".MOV", ".mov", ".M4V", ".m4v")
PHASE1_CONFIG = {
    "sample_interval": 5.0,
    "max_segment": 30.0,
    "min_segment": 5.0,
    "change_threshold": 0.19,
    "group_threshold": 0.24,
    "thumbnail_width": 640,
    "stt": "off",
}
APPLE_CONFIG = {
    "locales": ["ko-KR", "en-US"],
    "detector_sensitivity": "medium",
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def quick_fingerprint(path: Path, chunk_size: int = 1024 * 1024) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as stream:
        digest.update(stream.read(chunk_size))
        if size > chunk_size:
            stream.seek(max(0, size - chunk_size))
            digest.update(stream.read(chunk_size))
    return digest.hexdigest()


def stat_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def asset_id(relative_path: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(relative_path).stem).strip("-")
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:12]
    return f"{readable or 'video'}--{digest}"


@dataclass(frozen=True)
class ProxyAsset:
    asset_id: str
    relative_path: str
    original: Path
    proxy: Path
    manifest_event: dict[str, Any]


def load_latest_proxy_events(manifest: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    latest: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    if not manifest.is_file():
        return latest, [f"missing proxy manifest: {manifest}"]
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(f"ignored malformed manifest line {line_number}: {exc}")
            continue
        relative = event.get("relative_path")
        if isinstance(relative, str) and relative:
            latest[relative] = event
    return latest, warnings


def _mapped_original(source_root: Path, relative_path: str, proxy: Path) -> Path | None:
    declared = source_root / relative_path
    if declared.is_file():
        return declared
    relative_proxy = Path(relative_path)
    candidates = [
        source_root / relative_proxy.with_suffix(suffix)
        for suffix in VIDEO_SUFFIXES
    ]
    # A stale manifest may have an unexpected extension. Preserve the relative
    # directory and stem, but never search outside the immutable source root.
    if proxy.suffix.lower() == ".mp4":
        candidates.extend(
            source_root / relative_proxy.parent / f"{proxy.stem}{suffix}"
            for suffix in VIDEO_SUFFIXES
        )
    existing = sorted({candidate for candidate in candidates if candidate.is_file()})
    return existing[0] if len(existing) == 1 else None


def inventory_completed_proxies(
    source_root: Path, proxy_root: Path, manifest: Path
) -> tuple[list[ProxyAsset], list[dict[str, str]], list[str]]:
    latest, warnings = load_latest_proxy_events(manifest)
    assets: list[ProxyAsset] = []
    excluded: list[dict[str, str]] = []
    represented_proxies: set[Path] = set()
    for relative_path, event in sorted(latest.items()):
        event_proxy = event.get("proxy")
        represented_proxies.add(
            Path(event_proxy).expanduser().resolve()
            if event_proxy
            else (proxy_root / Path(relative_path)).with_suffix(".mp4").resolve()
        )
        event_name = str(event.get("event", ""))
        if event_name not in COMPLETE_PROXY_EVENTS:
            excluded.append({"relative_path": relative_path, "reason": f"latest_event_{event_name}"})
            continue
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            excluded.append({"relative_path": relative_path, "reason": "unsafe_relative_path"})
            continue
        proxy_value = event.get("proxy")
        proxy = Path(proxy_value).expanduser().resolve() if proxy_value else (
            proxy_root / relative
        ).with_suffix(".mp4").resolve()
        if not is_within(proxy, proxy_root):
            excluded.append({"relative_path": relative_path, "reason": "proxy_outside_root"})
            continue
        if "partial" in proxy.stem.lower() or not proxy.is_file():
            excluded.append({"relative_path": relative_path, "reason": "proxy_missing_or_partial"})
            continue
        sibling_partial = proxy.with_name(f"{proxy.stem}.partial{proxy.suffix}")
        if sibling_partial.exists():
            excluded.append({"relative_path": relative_path, "reason": "partial_sibling_present"})
            continue
        expected_size = event.get("proxy_bytes")
        if expected_size is not None and proxy.stat().st_size != int(expected_size):
            excluded.append({"relative_path": relative_path, "reason": "proxy_size_changed"})
            continue
        original = _mapped_original(source_root, relative_path, proxy)
        if original is None:
            excluded.append({"relative_path": relative_path, "reason": "original_not_uniquely_mapped"})
            continue
        if not is_within(original, source_root):
            excluded.append({"relative_path": relative_path, "reason": "original_outside_root"})
            continue
        assets.append(
            ProxyAsset(
                asset_id=asset_id(str(original.relative_to(source_root))),
                relative_path=str(original.relative_to(source_root)),
                original=original,
                proxy=proxy,
                manifest_event=event,
            )
        )

    # A few pilot proxies predate proxy-manifest.jsonl. Include standalone final
    # files only when their mirrored relative path maps to exactly one original.
    # Lineage creation will ffprobe and fingerprint both files before processing.
    known_asset_proxies = {asset.proxy for asset in assets}
    for proxy in sorted(proxy_root.rglob("*")):
        if (
            not proxy.is_file()
            or proxy.suffix.lower() != ".mp4"
            or proxy.name.startswith("._")
            or "partial" in proxy.stem.lower()
        ):
            continue
        proxy = proxy.resolve()
        if proxy in represented_proxies or proxy in known_asset_proxies:
            continue
        relative_proxy = proxy.relative_to(proxy_root)
        sibling_partial = proxy.with_name(f"{proxy.stem}.partial{proxy.suffix}")
        if sibling_partial.exists():
            excluded.append(
                {"relative_path": str(relative_proxy), "reason": "partial_sibling_present"}
            )
            continue
        original = _mapped_original(source_root, str(relative_proxy), proxy)
        if original is None:
            excluded.append(
                {
                    "relative_path": str(relative_proxy),
                    "reason": "filesystem_proxy_original_not_uniquely_mapped",
                }
            )
            continue
        source_relative = str(original.relative_to(source_root))
        synthetic_event = {
            "event": "filesystem_discovered",
            "relative_path": source_relative,
            "proxy": str(proxy),
            "requires_lineage_validation": True,
        }
        assets.append(
            ProxyAsset(
                asset_id=asset_id(source_relative),
                relative_path=source_relative,
                original=original,
                proxy=proxy,
                manifest_event=synthetic_event,
            )
        )
    assets.sort(key=lambda item: item.relative_path)
    return assets, excluded, warnings


def lineage_path(analysis_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    return analysis_root / "lineage" / relative.parent / f"{relative.name}.lineage.json"


def phase1_output_root(analysis_root: Path, relative_path: str) -> Path:
    return analysis_root / "phase1" / Path(relative_path).parent


def apple_output_dir(analysis_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    return analysis_root / "apple-speech" / relative.parent / f"{relative.stem}__{relative.suffix[1:].lower()}"


def verify_lineage(path: Path, asset: ProxyAsset) -> tuple[bool, str]:
    try:
        value = load_json(path)
        if value.get("schema_version") != "travel-video-source-lineage/v1":
            return False, "wrong lineage schema"
        if value.get("source_relative_path") != asset.relative_path:
            return False, "source relative path mismatch"
        original = value.get("original", {})
        processing = value.get("processing_input", {})
        if Path(original.get("path", "")).resolve() != asset.original.resolve():
            return False, "original path mismatch"
        if Path(processing.get("path", "")).resolve() != asset.proxy.resolve():
            return False, "proxy path mismatch"
        if original.get("size") != asset.original.stat().st_size:
            return False, "original size mismatch"
        if processing.get("size") != asset.proxy.stat().st_size:
            return False, "proxy size mismatch"
        if original.get("quick_fingerprint") != quick_fingerprint(asset.original):
            return False, "original fingerprint mismatch"
        if processing.get("quick_fingerprint") != quick_fingerprint(asset.proxy):
            return False, "proxy fingerprint mismatch"
        mapping = value.get("time_mapping", {})
        if mapping.get("kind") != "identity" or float(mapping.get("duration_delta_seconds", 999)) > 0.25:
            return False, "time mapping is not a verified identity"
        return True, "verified"
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return False, str(exc)


def _all_existing(paths: list[str | Path]) -> bool:
    return bool(paths) and all(Path(path).is_file() for path in paths)


def verify_phase1(run_dir: Path, asset: ProxyAsset) -> tuple[bool, str]:
    try:
        run = load_json(run_dir / "run.json")
        timeline = load_json(run_dir / "timeline.machine.json")
        if run.get("status") != "complete":
            return False, "phase1 run is not complete"
        if timeline.get("schema_version") != "phase1-machine/v1":
            return False, "wrong phase1 schema"
        source = timeline.get("source", {})
        if Path(source.get("path", "")).resolve() != asset.proxy.resolve():
            return False, "phase1 source path mismatch"
        if source.get("quick_fingerprint") != quick_fingerprint(asset.proxy):
            return False, "phase1 source fingerprint mismatch"
        config = timeline.get("config", {})
        if any(config.get(key) != expected for key, expected in PHASE1_CONFIG.items()):
            return False, "phase1 config mismatch"
        samples = timeline.get("samples", [])
        segments = timeline.get("segments", [])
        sheets = timeline.get("contact_sheets", [])
        if not samples or not segments or not sheets:
            return False, "phase1 evidence is empty"
        if not _all_existing([sample.get("frame", "") for sample in samples]):
            return False, "sample frame missing"
        if not _all_existing([segment.get("representative_frame", "") for segment in segments]):
            return False, "representative frame missing"
        if not _all_existing([sheet.get("path", "") for sheet in sheets]):
            return False, "contact sheet missing"
        if not (run_dir / "review-packet.json").is_file() or not (run_dir / "timeline.html").is_file():
            return False, "phase1 packet or HTML missing"
        return True, "verified"
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return False, str(exc)


def verify_apple_stt(output_dir: Path, asset: ProxyAsset) -> tuple[bool, str]:
    try:
        run = load_json(output_dir / "run.json")
        transcript = load_json(output_dir / "transcript.apple.json")
        if run.get("status") != "complete":
            return False, "Apple STT run is not complete"
        source_fingerprint = quick_fingerprint(asset.proxy)
        if run.get("source", {}).get("quick_fingerprint") != source_fingerprint:
            return False, "Apple STT run fingerprint mismatch"
        if transcript.get("source", {}).get("quick_fingerprint") != source_fingerprint:
            return False, "Apple transcript fingerprint mismatch"
        if transcript.get("locales") != APPLE_CONFIG["locales"]:
            return False, "Apple locale list mismatch"
        detector = transcript.get("detector", {})
        if not detector.get("enabled") or detector.get("sensitivity") != "medium":
            return False, "Apple detector config mismatch"
        paths = transcript.get("paths", {})
        required = [paths.get("audio", ""), paths.get("normalized", ""), paths.get("activity", "")]
        raw = paths.get("raw", {})
        required.extend(raw.get(locale, "") for locale in APPLE_CONFIG["locales"])
        if not _all_existing(required):
            return False, "Apple STT intermediate missing"
        return True, "verified"
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return False, str(exc)


def discover_phase1_run(analysis_root: Path, asset: ProxyAsset) -> Path | None:
    root = phase1_output_root(analysis_root, asset.relative_path)
    fingerprint = quick_fingerprint(asset.proxy)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", asset.proxy.stem).strip("-")
    asset_root = root / f"{safe_stem}--{fingerprint[:10]}"
    if not asset_root.is_dir():
        return None
    valid: list[Path] = []
    for candidate in sorted(path.parent for path in asset_root.glob("*/run.json")):
        okay, _ = verify_phase1(candidate, asset)
        if okay:
            valid.append(candidate)
    return valid[0] if len(valid) == 1 else None


def run_logged(command: list[str], cwd: Path, log_path: Path) -> subprocess.CompletedProcess[str]:
    started = now()
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps({"started_at": started, "finished_at": now(), "command": command}, ensure_ascii=False)
        + "\n\n[stdout]\n"
        + result.stdout
        + "\n[stderr]\n"
        + result.stderr,
        encoding="utf-8",
    )
    if result.returncode:
        raise RuntimeError(f"command exited {result.returncode}; see {log_path}")
    return result


@dataclass(frozen=True)
class RunnerConfig:
    project_root: Path
    source_root: Path
    proxy_root: Path
    analysis_root: Path
    status_root: Path
    travel_video: Path
    lineage_script: Path
    retry_after: float


def _state_path(config: RunnerConfig, asset: ProxyAsset) -> Path:
    return config.status_root / "assets" / f"{asset.asset_id}.json"


def _load_state(config: RunnerConfig, asset: ProxyAsset) -> dict[str, Any]:
    path = _state_path(config, asset)
    if path.is_file():
        try:
            return load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {
        "schema_version": "proxy-analysis-asset/v1",
        "asset_id": asset.asset_id,
        "source_relative_path": asset.relative_path,
        "original": str(asset.original),
        "proxy": str(asset.proxy),
        "stages": {},
    }


def _save_state(config: RunnerConfig, asset: ProxyAsset, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    state["original_stat"] = stat_signature(asset.original)
    state["proxy_stat"] = stat_signature(asset.proxy)
    atomic_json(_state_path(config, asset), state)


def _stage_log(config: RunnerConfig, asset: ProxyAsset, stage: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return config.status_root / "logs" / asset.asset_id / f"{stamp}-{stage}.log"


def _mark(state: dict[str, Any], stage: str, status: str, **details: Any) -> None:
    state.setdefault("stages", {})[stage] = {"status": status, "at": now(), **details}


def _recent_failure(state: dict[str, Any], retry_after: float) -> bool:
    failures = [value for value in state.get("stages", {}).values() if value.get("status") == "failed"]
    if not failures:
        return False
    latest = max(failures, key=lambda item: item.get("at", ""))
    try:
        age = datetime.now(UTC).timestamp() - datetime.fromisoformat(latest["at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    return age < retry_after


def process_asset(config: RunnerConfig, asset: ProxyAsset) -> dict[str, Any]:
    state = _load_state(config, asset)
    state["status"] = "running"
    _save_state(config, asset, state)
    events = config.status_root / "events.jsonl"
    append_jsonl(events, {"event": "asset_started", "time": now(), "asset_id": asset.asset_id})
    try:
        lineage = lineage_path(config.analysis_root, asset.relative_path)
        lineage_ok, reason = verify_lineage(lineage, asset)
        if not lineage_ok:
            log = _stage_log(config, asset, "lineage")
            _mark(state, "lineage", "running", log=str(log), prior_validation=reason)
            _save_state(config, asset, state)
            run_logged(
                [
                    sys.executable,
                    str(config.lineage_script),
                    "--source-root",
                    str(config.source_root),
                    "--original",
                    str(asset.original),
                    "--processing-input",
                    str(asset.proxy),
                    "--working-root",
                    str(config.analysis_root.parent),
                    "--project-root",
                    str(config.project_root),
                    "--output",
                    str(lineage),
                ],
                config.project_root,
                log,
            )
            lineage_ok, reason = verify_lineage(lineage, asset)
            if not lineage_ok:
                raise RuntimeError(f"lineage verification failed: {reason}")
        _mark(state, "lineage", "complete", output=str(lineage), validation=reason)
        _save_state(config, asset, state)

        phase1_run = discover_phase1_run(config.analysis_root, asset)
        if phase1_run is None:
            log = _stage_log(config, asset, "phase1")
            _mark(state, "phase1", "running", log=str(log))
            _save_state(config, asset, state)
            result = run_logged(
                [
                    str(config.travel_video),
                    "phase1",
                    str(asset.proxy),
                    "--output-root",
                    str(phase1_output_root(config.analysis_root, asset.relative_path)),
                    "--sample-interval",
                    "5",
                    "--max-segment",
                    "30",
                    "--min-segment",
                    "5",
                    "--stt",
                    "off",
                ],
                config.project_root,
                log,
            )
            output_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            phase1_run = Path(output_lines[-1]).resolve() if output_lines else None
        if phase1_run is None:
            raise RuntimeError("phase1 did not report a run directory")
        phase1_ok, reason = verify_phase1(phase1_run, asset)
        if not phase1_ok:
            raise RuntimeError(f"phase1 verification failed: {reason}")
        _mark(state, "phase1", "complete", output=str(phase1_run), validation=reason)
        _save_state(config, asset, state)

        apple_dir = apple_output_dir(config.analysis_root, asset.relative_path)
        apple_ok, reason = verify_apple_stt(apple_dir, asset)
        if not apple_ok:
            log = _stage_log(config, asset, "apple-stt")
            _mark(state, "apple_stt", "running", log=str(log), prior_validation=reason)
            _save_state(config, asset, state)
            # SpeechAnalyzer/asset inventory is not reliably re-entrant across
            # simultaneous locale jobs. Keep Phase 1 concurrent but serialize
            # Apple STT across archive workers.
            apple_lock = config.status_root / "apple-stt.lock"
            with apple_lock.open("a+", encoding="utf-8") as lock_stream:
                fcntl.flock(lock_stream, fcntl.LOCK_EX)
                run_logged(
                    [
                        str(config.travel_video),
                        "stt-apple",
                        str(asset.proxy),
                        "--output-dir",
                        str(apple_dir),
                        "--locales",
                        "ko-KR,en-US",
                        "--detector-sensitivity",
                        "medium",
                    ],
                    config.project_root,
                    log,
                )
            apple_ok, reason = verify_apple_stt(apple_dir, asset)
            if not apple_ok:
                raise RuntimeError(f"Apple STT verification failed: {reason}")
        _mark(
            state,
            "apple_stt",
            "complete",
            output=str(apple_dir / "transcript.apple.json"),
            validation=reason,
        )
        state["status"] = "complete"
        state["completed_at"] = now()
        _save_state(config, asset, state)
        append_jsonl(events, {"event": "asset_completed", "time": now(), "asset_id": asset.asset_id})
        return state
    except Exception as exc:  # noqa: BLE001 - persist per-asset failure and continue batch.
        state["status"] = "failed"
        state["error"] = str(exc)
        _mark(state, "asset", "failed", error=str(exc))
        _save_state(config, asset, state)
        append_jsonl(
            events,
            {"event": "asset_failed", "time": now(), "asset_id": asset.asset_id, "error": str(exc)},
        )
        return state


def verified_complete(config: RunnerConfig, asset: ProxyAsset) -> bool:
    state = _load_state(config, asset)
    if state.get("status") != "complete":
        return False
    if state.get("original_stat") != stat_signature(asset.original):
        return False
    if state.get("proxy_stat") != stat_signature(asset.proxy):
        return False
    lineage_ok, _ = verify_lineage(lineage_path(config.analysis_root, asset.relative_path), asset)
    phase_path = state.get("stages", {}).get("phase1", {}).get("output")
    apple_path = state.get("stages", {}).get("apple_stt", {}).get("output")
    if not phase_path or not apple_path:
        return False
    phase_ok, _ = verify_phase1(Path(phase_path), asset)
    apple_ok, _ = verify_apple_stt(Path(apple_path).parent, asset)
    return lineage_ok and phase_ok and apple_ok


def write_inventory(
    config: RunnerConfig,
    assets: list[ProxyAsset],
    excluded: list[dict[str, str]],
    warnings: list[str],
) -> None:
    atomic_json(
        config.status_root / "inventory.json",
        {
            "schema_version": "proxy-analysis-inventory/v1",
            "generated_at": now(),
            "source_root": str(config.source_root),
            "proxy_root": str(config.proxy_root),
            "analysis_root": str(config.analysis_root),
            "eligible_count": len(assets),
            "excluded_count": len(excluded),
            "assets": [
                {
                    "asset_id": asset.asset_id,
                    "source_relative_path": asset.relative_path,
                    "original": str(asset.original),
                    "proxy": str(asset.proxy),
                    "proxy_event": asset.manifest_event.get("event"),
                }
                for asset in assets
            ],
            "excluded": excluded,
            "warnings": warnings,
        },
    )


def run_cycle(config: RunnerConfig, jobs: int, limit: int | None, dry_run: bool) -> dict[str, Any]:
    assets, excluded, warnings = inventory_completed_proxies(
        config.source_root,
        config.proxy_root,
        config.proxy_root / "proxy-manifest.jsonl",
    )
    write_inventory(config, assets, excluded, warnings)
    complete: list[ProxyAsset] = []
    retry_delayed: list[ProxyAsset] = []
    queued: list[ProxyAsset] = []
    for asset in assets:
        if verified_complete(config, asset):
            complete.append(asset)
            continue
        state = _load_state(config, asset)
        if _recent_failure(state, config.retry_after):
            retry_delayed.append(asset)
        else:
            queued.append(asset)
    if limit is not None:
        queued = queued[:limit]

    results: list[dict[str, Any]] = []
    if not dry_run and queued:
        with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="proxy-analysis") as executor:
            futures = {executor.submit(process_asset, config, asset): asset for asset in queued}
            for future in as_completed(futures):
                results.append(future.result())
    summary = {
        "schema_version": "proxy-analysis-summary/v1",
        "updated_at": now(),
        "eligible": len(assets),
        "verified_complete_before_cycle": len(complete),
        "queued": len(queued),
        "retry_delayed": len(retry_delayed),
        "completed_this_cycle": sum(item.get("status") == "complete" for item in results),
        "failed_this_cycle": sum(item.get("status") == "failed" for item in results),
        "dry_run": dry_run,
    }
    atomic_json(config.status_root / "summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--proxy-root",
        type=Path,
        default=Path("/Volumes/ExternalSSD/travel-video-editor/proxies/1080p-h264"),
    )
    parser.add_argument(
        "--analysis-root",
        type=Path,
        default=Path("/Volumes/ExternalSSD/travel-video-editor/analysis"),
    )
    parser.add_argument("--status-root", type=Path, default=Path("work/full-batch"))
    parser.add_argument("--jobs", type=int, default=2, help="Concurrent videos (1-8)")
    parser.add_argument("--limit", type=int, help="Maximum newly queued videos this cycle")
    parser.add_argument("--watch", action="store_true", help="Poll for newly completed proxies")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--retry-after", type=float, default=3600.0)
    parser.add_argument("--dry-run", action="store_true", help="Inventory and plan without media work")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> RunnerConfig:
    project_root = args.project_root.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    proxy_root = args.proxy_root.expanduser().resolve()
    analysis_root = args.analysis_root.expanduser().resolve()
    status_root = args.status_root.expanduser()
    if not status_root.is_absolute():
        status_root = project_root / status_root
    status_root = status_root.resolve()
    if not source_root.is_dir() or not proxy_root.is_dir():
        raise ValueError("source and proxy roots must be mounted directories")
    for output in (analysis_root, status_root):
        if is_within(output, source_root):
            raise ValueError(f"refusing to write under immutable source root: {output}")
    if source_root == proxy_root or is_within(proxy_root, source_root):
        raise ValueError("proxy root must be outside immutable source root")
    if not 1 <= args.jobs <= 8:
        raise ValueError("--jobs must be between 1 and 8")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be positive")
    if args.poll_seconds < 5:
        raise ValueError("--poll-seconds must be at least 5")
    if args.retry_after < 0:
        raise ValueError("--retry-after must not be negative")
    travel_video = project_root / ".venv" / "bin" / "travel-video"
    lineage_script = project_root / "skills/travel-video-pipeline/scripts/create_lineage_manifest.py"
    if not travel_video.is_file() or not lineage_script.is_file():
        raise ValueError("project CLI or lineage helper is missing")
    analysis_root.mkdir(parents=True, exist_ok=True)
    status_root.mkdir(parents=True, exist_ok=True)
    return RunnerConfig(
        project_root=project_root,
        source_root=source_root,
        proxy_root=proxy_root,
        analysis_root=analysis_root,
        status_root=status_root,
        travel_video=travel_video,
        lineage_script=lineage_script,
        retry_after=args.retry_after,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config(args)
        lock_path = config.status_root / "batch.lock"
        lock_stream = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another archive preparation runner holds {lock_path}") from exc
        atomic_json(
            config.status_root / "config.json",
            {
                "schema_version": "proxy-analysis-config/v1",
                "updated_at": now(),
                "project_root": str(config.project_root),
                "source_root": str(config.source_root),
                "proxy_root": str(config.proxy_root),
                "analysis_root": str(config.analysis_root),
                "jobs": args.jobs,
                "phase1": PHASE1_CONFIG,
                "apple_stt": APPLE_CONFIG,
            },
        )
        stopped = False

        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        while not stopped:
            summary = run_cycle(config, args.jobs, args.limit, args.dry_run)
            print(json.dumps(summary, ensure_ascii=False))
            if not args.watch or args.dry_run:
                break
            deadline = time.monotonic() + args.poll_seconds
            while not stopped and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
