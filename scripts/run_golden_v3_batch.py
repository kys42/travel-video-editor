#!/usr/bin/env python3
"""Prepare Golden v3 multimodal review packets by accepted story day.

This is a deterministic, prepare-only orchestrator.  It consumes the story-day
preflight manifest, reuses only accepted quick grouping timelines, runs local
boundary/visual/context producers, and stops at the fresh integrated review
packet.  It never performs model review, merges a review, renders an edit, or
modifies source/proxy media.

The boundary and visual-moment commands are supplied by the multimodal
boundary feature.  Keeping this runner separate lets its fake-runner tests pass
on main and makes it executable as soon as that feature is merged.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator


STORY_DAYS_SCHEMAS = {
    "travel-video-story-day-preflight/v1",
    "travel-video-story-day-preflight/v2",
}
OVERRIDES_SCHEMA = "golden-v3-grouping-overrides/v1"
STATE_SCHEMA = "golden-v3-preparation-asset-state/v1"
MANIFEST_SCHEMA = "golden-v3-preparation-manifest/v1"
DAY_MANIFEST_SCHEMA = "golden-v3-preparation-day-manifest/v1"
VALIDATION_SCHEMA = "golden-v3-evidence-validation/v1"
SAFE_ASSET_ID = re.compile(r"^[A-Za-z0-9._-]+$")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
Runner = Callable[[list[str], Path], dict[str, str]]


class StageLockedError(RuntimeError):
    """Raised when another process owns the same asset/stage lock."""


@dataclass(frozen=True)
class GoldenV3Config:
    clip_evidence: bool = False
    max_frames: int = 16
    max_window: float = 8.0
    vision_interval: float = 1 / 3
    ocr_interval: float = 1.0
    motion_interval: float = 1 / 3
    ffmpeg_scene_threshold: float = 0.30
    cluster_tolerance: float = 0.55
    neighbor_context: float = 5.0
    feature_distance_floor: float = 0.12
    feature_distance_quantile: float = 0.90
    motion_delta_quantile: float = 0.92
    visual_moment_window: float = 6.0
    visual_moment_nms: float = 3.0
    visual_moments_per_minute: float = 6.0
    visual_moment_thumbnail_width: int = 960

    def validate(self) -> None:
        positive = {
            "max_frames": self.max_frames,
            "max_window": self.max_window,
            "vision_interval": self.vision_interval,
            "ocr_interval": self.ocr_interval,
            "motion_interval": self.motion_interval,
            "cluster_tolerance": self.cluster_tolerance,
            "neighbor_context": self.neighbor_context,
            "visual_moment_window": self.visual_moment_window,
            "visual_moment_nms": self.visual_moment_nms,
            "visual_moments_per_minute": self.visual_moments_per_minute,
            "visual_moment_thumbnail_width": self.visual_moment_thumbnail_width,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        unit_interval = {
            "ffmpeg_scene_threshold": self.ffmpeg_scene_threshold,
            "feature_distance_floor": self.feature_distance_floor,
            "feature_distance_quantile": self.feature_distance_quantile,
            "motion_delta_quantile": self.motion_delta_quantile,
        }
        for name, value in unit_interval.items():
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")

    def boundary_cli_args(self) -> list[str]:
        values = (
            ("--vision-interval", self.vision_interval),
            ("--ocr-interval", self.ocr_interval),
            ("--motion-interval", self.motion_interval),
            ("--ffmpeg-scene-threshold", self.ffmpeg_scene_threshold),
            ("--cluster-tolerance", self.cluster_tolerance),
            ("--neighbor-context", self.neighbor_context),
            ("--feature-distance-floor", self.feature_distance_floor),
            ("--feature-distance-quantile", self.feature_distance_quantile),
            ("--motion-delta-quantile", self.motion_delta_quantile),
            ("--visual-moment-window", self.visual_moment_window),
            ("--visual-moment-nms", self.visual_moment_nms),
            ("--visual-moments-per-minute", self.visual_moments_per_minute),
            ("--visual-moment-thumbnail-width", self.visual_moment_thumbnail_width),
        )
        return [str(item) for pair in values for item in pair]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def normalized_path(value: str | Path) -> str:
    return unicodedata.normalize("NFC", str(Path(value).expanduser().resolve()))


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def file_record(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    stat = path.stat()
    return {
        "kind": "file",
        "path": str(path),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(path),
    }


def media_record(path: Path, quick_fingerprint: str) -> dict[str, Any]:
    path = path.expanduser().resolve()
    stat = path.stat()
    if not quick_fingerprint:
        raise ValueError(f"Missing verified quick fingerprint for media: {path}")
    return {
        "kind": "media",
        "path": str(path),
        "size_bytes": stat.st_size,
        "quick_fingerprint": quick_fingerprint,
        "fingerprint_algorithm": "phase1-size+first-last-1MiB/sha256",
    }


def directory_record(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"Missing output directory: {path}")
    files = sorted(item for item in path.rglob("*") if item.is_file())
    entries = [
        {
            "relative_path": item.relative_to(path).as_posix(),
            "size_bytes": item.stat().st_size,
            "sha256": sha256_file(item),
        }
        for item in files
    ]
    return {
        "kind": "directory",
        "path": str(path),
        "file_count": len(entries),
        "tree_sha256": canonical_digest(entries),
    }


def record_is_current(record: dict[str, Any]) -> bool:
    try:
        path = Path(str(record["path"]))
        if record.get("kind") == "file":
            return (
                path.is_file()
                and path.stat().st_size == record.get("size_bytes")
                and sha256_file(path) == record.get("sha256")
            )
        if record.get("kind") == "directory":
            current = directory_record(path)
            return current["file_count"] == record.get("file_count") and current[
                "tree_sha256"
            ] == record.get("tree_sha256")
    except (OSError, KeyError, ValueError):
        return False
    return False


def cli_prefix() -> list[str]:
    return [sys.executable, "-m", "travel_video.cli"]


def run_cli(command: list[str], cwd: Path) -> dict[str, str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def error_text(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        detail = (error.stderr or error.stdout or "").strip()
        if detail:
            return f"{error}: {detail}"
    return str(error)


@contextmanager
def nonblocking_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StageLockedError(f"Stage lock is already held: {path}") from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired_at={utc_now()}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def implementation_record() -> dict[str, Any]:
    producer_candidates = [
        PROJECT_ROOT / "src/travel_video/cli.py",
        PROJECT_ROOT / "src/travel_video/context.py",
        PROJECT_ROOT / "src/travel_video/scene_dialogue.py",
        PROJECT_ROOT / "src/travel_video/clip_evidence.py",
        PROJECT_ROOT / "src/travel_video/boundary_proposals.py",
        PROJECT_ROOT / "scripts/apple_vision_boundary_signals.swift",
    ]
    orchestrator_path = Path(__file__).resolve()
    files = []
    for path in producer_candidates:
        if not path.is_file():
            continue
        record = file_record(path)
        record["component"] = path.relative_to(PROJECT_ROOT).as_posix()
        files.append(record)
    missing = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in producer_candidates
        if not path.is_file()
    ]
    orchestrator = file_record(orchestrator_path)
    orchestrator["component"] = orchestrator_path.relative_to(PROJECT_ROOT).as_posix()
    return {
        # Stage reuse follows the actual deterministic producers, their inputs,
        # and stage config. CLI orchestration-only edits must not invalidate
        # hours of Vision/FFmpeg evidence that those producers did not change.
        "digest": canonical_digest(
            {
                "files": [
                    {"component": item["component"], "sha256": item["sha256"]}
                    for item in files
                ],
                "missing": missing,
            }
        ),
        "files": files,
        "missing": missing,
        "orchestrator": orchestrator,
        "orchestrator_digest": orchestrator["sha256"],
    }


def load_grouping_overrides(
    path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if path is None:
        return {}, None
    path = path.expanduser().resolve()
    payload = load_json(path)
    if payload.get("schema_version") != OVERRIDES_SCHEMA:
        raise ValueError(f"Expected grouping override schema {OVERRIDES_SCHEMA}")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Grouping override assets must be a list")
    overrides: dict[str, Any] = {}
    for entry in assets:
        if not isinstance(entry, dict):
            raise ValueError("Every grouping override must be an object")
        relative_path = str(entry.get("source_relative_path") or "")
        if not relative_path:
            raise ValueError("Grouping override is missing source_relative_path")
        if relative_path in overrides:
            raise ValueError(f"Duplicate grouping override: {relative_path}")
        overrides[relative_path] = entry
    return overrides, file_record(path)


def _segment_projection(segment: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "segment_id",
        "start",
        "end",
        "start_timecode",
        "end_timecode",
        "boundary_reason",
        "representative_sample_id",
        "representative_frame",
        "representative_hash",
        "machine",
        "machine_group_id",
        "contact_sheet",
        "contact_cell",
    )
    return {field: segment.get(field) for field in fields}


def validate_quick_timeline(
    asset: dict[str, Any], timeline_path: Path
) -> dict[str, Any]:
    paths = asset.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("Asset paths must be an object")
    machine_value = paths.get("phase1_machine")
    if not machine_value:
        raise ValueError("Asset is missing paths.phase1_machine")
    machine_path = Path(str(machine_value)).expanduser().resolve()
    timeline_path = timeline_path.expanduser().resolve()
    if timeline_path.name != "timeline.reviewed.json":
        raise ValueError("Accepted grouping path must name timeline.reviewed.json")
    timeline = load_json(timeline_path)
    machine = load_json(machine_path)
    if timeline.get("schema_version") != "phase1-reviewed-timeline/v1":
        raise ValueError("Accepted grouping is not phase1-reviewed-timeline/v1")
    if machine.get("schema_version") != "phase1-machine/v1":
        raise ValueError("Phase 1 input is not phase1-machine/v1")
    if timeline.get("asset_id") != machine.get("asset_id"):
        raise ValueError("Reviewed and machine timeline asset_id differ")
    asset_id = str(timeline.get("asset_id") or "")
    if not SAFE_ASSET_ID.fullmatch(asset_id):
        raise ValueError(f"Unsafe timeline asset_id: {asset_id!r}")
    if timeline.get("source") != machine.get("source"):
        raise ValueError("Reviewed timeline source differs from machine timeline")
    if timeline.get("media") != machine.get("media"):
        raise ValueError("Reviewed timeline media differs from machine timeline")
    proxy_value = paths.get("proxy")
    if not proxy_value or normalized_path(
        timeline["source"]["path"]
    ) != normalized_path(str(proxy_value)):
        raise ValueError("Reviewed timeline source path differs from verified proxy")
    expected_fingerprint = str(asset.get("proxy_quick_fingerprint") or "")
    if timeline["source"].get("quick_fingerprint") != expected_fingerprint:
        raise ValueError("Reviewed timeline fingerprint differs from preflight proxy")
    duration = float(timeline["media"]["duration"])
    expected_duration = float(asset["duration_seconds"])
    if abs(duration - expected_duration) > 0.05:
        raise ValueError("Reviewed timeline duration differs from preflight asset")
    reviewed_segments = timeline.get("segments")
    machine_segments = machine.get("segments")
    if not isinstance(reviewed_segments, list) or not isinstance(
        machine_segments, list
    ):
        raise ValueError("Timeline segments must be lists")
    if [_segment_projection(item) for item in reviewed_segments] != [
        _segment_projection(item) for item in machine_segments
    ]:
        raise ValueError("Reviewed timeline changed immutable machine segment fields")
    segment_ids = [str(item.get("segment_id") or "") for item in machine_segments]
    groups = timeline.get("reviewed_groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("Reviewed timeline has no reviewed_groups")
    grouped_ids: list[str] = []
    seen_group_ids: set[str] = set()
    for group in groups:
        group_id = str(group.get("group_id") or "")
        if not group_id or group_id in seen_group_ids:
            raise ValueError("Reviewed timeline group IDs are missing or duplicated")
        seen_group_ids.add(group_id)
        member_ids = group.get("segment_ids")
        if not isinstance(member_ids, list) or not member_ids:
            raise ValueError(f"Reviewed group {group_id} has no segment IDs")
        grouped_ids.extend(str(item) for item in member_ids)
    if grouped_ids != segment_ids:
        raise ValueError("Reviewed groups must cover machine segments exactly in order")
    return {
        "asset_id": asset_id,
        "timeline": timeline,
        "machine_path": machine_path,
        "machine": machine,
    }


def resolve_quick_timeline(
    asset: dict[str, Any], overrides: dict[str, Any]
) -> tuple[Path, str, dict[str, Any]]:
    relative_path = str(asset["source_relative_path"])
    override = overrides.get(relative_path)
    if override is not None:
        if override.get("accepted") is not True:
            raise ValueError("Golden v3 grouping override is not explicitly accepted")
        value = override.get("timeline_reviewed_path")
        if not value:
            raise ValueError("Accepted grouping override has no timeline_reviewed_path")
        path = Path(str(value)).expanduser().resolve()
        expected_sha = override.get("timeline_sha256")
        if expected_sha and sha256_file(path) != expected_sha:
            raise ValueError("Accepted grouping override digest does not match")
        result = validate_quick_timeline(asset, path)
        return path, "golden_v3_accepted_override", result

    legacy = asset.get("legacy_downstream_cache")
    if not isinstance(legacy, dict) or legacy.get("quick_group_review") is not True:
        raise ValueError(
            "No accepted Golden v3 grouping override or verified legacy grouping"
        )
    run_dir = asset.get("paths", {}).get("phase1_run_dir")
    if not run_dir:
        raise ValueError("Verified legacy grouping has no Phase 1 run directory")
    path = Path(str(run_dir)).expanduser().resolve() / "timeline.reviewed.json"
    result = validate_quick_timeline(asset, path)
    return path, "preflight_verified_legacy", result


def select_days(
    manifest: dict[str, Any], requested: list[str], excluded: list[str]
) -> list[str]:
    days = manifest.get("story_days")
    if not isinstance(days, list):
        raise ValueError("story_days must be a list")
    available = [str(item.get("story_day") or "") for item in days]
    if any(not item for item in available) or len(set(available)) != len(available):
        raise ValueError("Story-day names are missing or duplicated")
    unknown_requested = sorted(set(requested) - set(available))
    unknown_excluded = sorted(set(excluded) - set(available))
    if unknown_requested:
        raise ValueError(f"Unknown story day(s): {', '.join(unknown_requested)}")
    if unknown_excluded:
        raise ValueError(
            f"Unknown excluded story day(s): {', '.join(unknown_excluded)}"
        )
    selected = requested or available
    selected = [item for item in selected if item not in set(excluded)]
    if not selected:
        raise ValueError("No story days selected")
    return selected


def select_assets(
    manifest: dict[str, Any], story_days: list[str], asset_regex: str | None
) -> list[dict[str, Any]]:
    assets = manifest.get("assets")
    days = manifest.get("story_days")
    if not isinstance(assets, list) or not isinstance(days, list):
        raise ValueError("Manifest assets and story_days must be lists")
    day_map = {str(item["story_day"]): item for item in days}
    pattern = re.compile(asset_regex) if asset_regex else None
    selected: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for raw in assets:
        if not isinstance(raw, dict):
            raise ValueError("Every preflight asset must be an object")
        relative_path = str(raw.get("source_relative_path") or "")
        if not relative_path or relative_path in seen_paths:
            raise ValueError(
                f"Missing or duplicate source_relative_path: {relative_path!r}"
            )
        seen_paths.add(relative_path)
        day = str(raw.get("story_day") or "")
        if day not in story_days:
            continue
        if pattern and not pattern.search(relative_path):
            continue
        eligible_members = day_map[day].get("eligible_assets")
        if not isinstance(eligible_members, list):
            eligible_members = day_map[day].get("practical_ready_assets")
        if not isinstance(eligible_members, list):
            raise ValueError(
                f"Story day {day} must provide eligible_assets or "
                "practical_ready_assets"
            )
        listed = relative_path in eligible_members
        if "eligible_for_golden_v3_extraction" in raw:
            eligible = raw.get("eligible_for_golden_v3_extraction") is True
        else:
            eligible = raw.get("practical_ready") is True
        if listed != eligible:
            raise ValueError(
                f"Story-day eligible list disagrees with asset status: {relative_path}"
            )
        selected.append(raw)
    selected.sort(key=lambda item: (str(item["story_day"]), str(item["local_capture"])))
    return selected


def asset_id_from_machine(asset: dict[str, Any]) -> str:
    value = asset.get("paths", {}).get("phase1_machine")
    if value:
        try:
            asset_id = str(load_json(Path(str(value)))["asset_id"])
            if SAFE_ASSET_ID.fullmatch(asset_id):
                return asset_id
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
    relative_path = str(asset.get("source_relative_path") or "asset")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(relative_path).stem) or "asset"
    return f"{stem}--{hashlib.sha256(relative_path.encode()).hexdigest()[:12]}"


def validate_unique_asset_ids(assets: list[dict[str, Any]]) -> None:
    owners: dict[str, str] = {}
    for asset in assets:
        asset_id = asset_id_from_machine(asset)
        relative_path = str(asset["source_relative_path"])
        previous = owners.get(asset_id)
        if previous is not None and previous != relative_path:
            raise ValueError(
                f"Duplicate asset_id {asset_id}: {previous}, {relative_path}"
            )
        owners[asset_id] = relative_path


def output_root_is_safe(manifest: dict[str, Any], output_root: Path) -> None:
    output_root = output_root.expanduser().resolve()
    for key in ("source_root", "proxy_root"):
        value = manifest.get(key)
        if value and is_relative_to(
            output_root, Path(str(value)).expanduser().resolve()
        ):
            raise ValueError(f"Output root must not be inside {key}: {output_root}")


def stage_signature(
    name: str,
    inputs: dict[str, Any],
    config: dict[str, Any],
    implementation_digest: str,
) -> str:
    return canonical_digest(
        {
            "stage": name,
            "inputs": inputs,
            "config": config,
            "implementation_digest": implementation_digest,
        }
    )


def reusable_stage(stage: dict[str, Any] | None, signature: str) -> bool:
    if not isinstance(stage, dict):
        return False
    if stage.get("status") not in {"completed", "reused"}:
        return False
    if stage.get("signature") != signature:
        return False
    outputs = stage.get("outputs")
    return isinstance(outputs, dict) and all(
        isinstance(record, dict) and record_is_current(record)
        for record in outputs.values()
    )


def update_state(state_path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_json(state_path, state)


def execute_stage(
    *,
    asset_root: Path,
    state_path: Path,
    state: dict[str, Any],
    name: str,
    signature: str,
    inputs: dict[str, Any],
    config: dict[str, Any],
    commands: list[list[str]],
    collect_outputs: Callable[[], dict[str, dict[str, Any]]],
    runner: Runner,
    after_commands: Callable[[list[dict[str, str]]], None] | None = None,
) -> str:
    lock_path = asset_root / "locks" / f"{name}.lock"
    with nonblocking_lock(lock_path):
        previous = state["stages"].get(name)
        if reusable_stage(previous, signature):
            reused = {**previous, "status": "reused", "last_reused_at": utc_now()}
            state["stages"][name] = reused
            update_state(state_path, state)
            return "reused"
        started_at = utc_now()
        stage = {
            "stage_id": name,
            "status": "running",
            "signature": signature,
            "started_at": started_at,
            "inputs": inputs,
            "config": config,
            "config_digest": canonical_digest(config),
            "implementation_digest": state["implementation"]["digest"],
            "commands": commands,
        }
        state["stages"][name] = stage
        update_state(state_path, state)
        try:
            command_results = [runner(command, PROJECT_ROOT) for command in commands]
            if after_commands is not None:
                after_commands(command_results)
            outputs = collect_outputs()
            completed = {
                **stage,
                "status": "completed",
                "completed_at": utc_now(),
                "command_results": [
                    {
                        "stdout": result.get("stdout", "")[-4000:],
                        "stderr": result.get("stderr", "")[-4000:],
                    }
                    for result in command_results
                ],
                "outputs": outputs,
            }
            state["stages"][name] = completed
            update_state(state_path, state)
            return "completed"
        except Exception as error:
            failed = {
                **stage,
                "status": "failed",
                "completed_at": utc_now(),
                "error": {
                    "type": type(error).__name__,
                    "message": error_text(error),
                },
            }
            state["stages"][name] = failed
            state["status"] = "failed"
            update_state(state_path, state)
            raise


def _base_inputs(
    asset: dict[str, Any], timeline_path: Path, override_manifest: dict[str, Any] | None
) -> dict[str, Any]:
    paths = asset["paths"]
    proxy_path = Path(str(paths["proxy"])).expanduser().resolve()
    lineage_path = Path(str(paths["lineage"])).expanduser().resolve()
    apple_path = Path(str(paths["apple_transcript"])).expanduser().resolve()
    machine_path = Path(str(paths["phase1_machine"])).expanduser().resolve()
    return {
        "source_relative_path": str(asset["source_relative_path"]),
        "story_day": str(asset["story_day"]),
        "original": {
            "path": str(paths["original"]),
            "quick_fingerprint": str(asset["source_quick_fingerprint"]),
        },
        "processing_input": media_record(
            proxy_path, str(asset["proxy_quick_fingerprint"])
        ),
        "lineage": file_record(lineage_path),
        "timeline_machine": file_record(machine_path),
        "timeline_reviewed": file_record(timeline_path),
        "apple_transcript": file_record(apple_path),
        "grouping_override_manifest": override_manifest,
    }


def prepare_asset(
    asset: dict[str, Any],
    output_root: Path,
    overrides: dict[str, Any],
    override_manifest: dict[str, Any] | None,
    implementation: dict[str, Any],
    config: GoldenV3Config,
    runner: Runner,
) -> dict[str, Any]:
    asset_id = asset_id_from_machine(asset)
    relative_path = str(asset["source_relative_path"])
    story_day = str(asset["story_day"])
    asset_root = output_root / "assets" / asset_id
    state_path = asset_root / "state.json"
    eligible = (
        asset.get("eligible_for_golden_v3_extraction") is True
        if "eligible_for_golden_v3_extraction" in asset
        else asset.get("practical_ready") is True
    )
    if not eligible:
        return {
            "asset_id": asset_id,
            "source_relative_path": relative_path,
            "story_day": story_day,
            "status": "excluded",
            "reason": "preflight_ineligible",
            "missing_prerequisites": asset.get("missing_prerequisites", []),
        }
    try:
        timeline_path, timeline_origin, timeline_result = resolve_quick_timeline(
            asset, overrides
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        blocked = {
            "schema_version": STATE_SCHEMA,
            "asset_id": asset_id,
            "source_relative_path": relative_path,
            "story_day": story_day,
            "status": "blocked",
            "updated_at": utc_now(),
            "reason": "accepted_quick_grouping_unavailable",
            "error": {"type": type(error).__name__, "message": error_text(error)},
            "stages": {},
        }
        try:
            with nonblocking_lock(asset_root / "locks/resolution.lock"):
                atomic_json(state_path, blocked)
        except StageLockedError:
            return {
                "asset_id": asset_id,
                "source_relative_path": relative_path,
                "story_day": story_day,
                "status": "locked",
                "stage": "resolution",
            }
        return {
            "asset_id": asset_id,
            "source_relative_path": relative_path,
            "story_day": story_day,
            "status": "blocked",
            "reason": blocked["reason"],
            "error": blocked["error"],
            "state": str(state_path),
        }

    if timeline_result["asset_id"] != asset_id:
        raise ValueError(
            f"Resolved timeline asset ID changed unexpectedly: {relative_path}"
        )
    try:
        inputs = _base_inputs(asset, timeline_path, override_manifest)
        config_value = asdict(config)
        run_signature = canonical_digest(
            {
                "inputs": inputs,
                "config": config_value,
                "implementation_digest": implementation["digest"],
            }
        )
        with nonblocking_lock(asset_root / "locks/resolution.lock"):
            if state_path.is_file():
                state = load_json(state_path)
                if state.get("schema_version") != STATE_SCHEMA:
                    raise ValueError(f"Incompatible asset state: {state_path}")
                if state.get("source_relative_path") != relative_path:
                    raise ValueError(
                        f"Asset state belongs to another source: {state_path}"
                    )
            else:
                state = {"schema_version": STATE_SCHEMA, "stages": {}}
            state.update(
                {
                    "asset_id": asset_id,
                    "source_relative_path": relative_path,
                    "story_day": story_day,
                    "status": "running",
                    "attempt_id": str(uuid.uuid4()),
                    "run_signature": run_signature,
                    "inputs": inputs,
                    "grouping": {
                        "origin": timeline_origin,
                        "timeline_reviewed_path": str(timeline_path),
                        "accepted": True,
                    },
                    "implementation": implementation,
                    "config": config_value,
                    "config_digest": canonical_digest(config_value),
                    "policy": {
                        "prepare_only": True,
                        "model_review_performed": False,
                        "review_merge_performed": False,
                        "edit_render_performed": False,
                        "source_or_proxy_media_modified": False,
                    },
                }
            )
            update_state(state_path, state)

        proxy_path = Path(inputs["processing_input"]["path"])
        lineage_path = Path(inputs["lineage"]["path"])
        apple_path = Path(inputs["apple_transcript"]["path"])

        boundary_inputs = {
            key: inputs[key]
            for key in (
                "processing_input",
                "lineage",
                "timeline_reviewed",
                "apple_transcript",
            )
        }
        boundary_config = {
            key: value
            for key, value in config_value.items()
            if key not in {"max_frames", "max_window"}
        }
        boundary_signature = stage_signature(
            "boundaries",
            boundary_inputs,
            boundary_config,
            implementation["digest"],
        )
        boundary_dir = asset_root / "boundaries" / boundary_signature[:16]
        boundary_command = [
            *cli_prefix(),
            "build-boundary-proposals",
            str(proxy_path),
            str(timeline_path),
            str(apple_path),
            "--output-dir",
            str(boundary_dir),
            "--lineage",
            str(lineage_path),
            *config.boundary_cli_args(),
        ]

        def boundary_outputs() -> dict[str, dict[str, Any]]:
            return {
                "run_intent": file_record(boundary_dir / "run-intent.json"),
                "run": file_record(boundary_dir / "run.json"),
                "boundary_proposals": file_record(boundary_dir / "proposals.json"),
                "visual_moments": file_record(boundary_dir / "visual-moments.json"),
                "signals": directory_record(boundary_dir / "signals"),
                "visual_moment_frames": directory_record(
                    boundary_dir / "visual-moment-frames"
                ),
            }

        statuses: list[str] = []
        statuses.append(
            execute_stage(
                asset_root=asset_root,
                state_path=state_path,
                state=state,
                name="boundaries",
                signature=boundary_signature,
                inputs=boundary_inputs,
                config=boundary_config,
                commands=[boundary_command],
                collect_outputs=boundary_outputs,
                runner=runner,
            )
        )
        proposals_path = boundary_dir / "proposals.json"
        visual_moments_path = boundary_dir / "visual-moments.json"

        validation_inputs = {
            "timeline_reviewed": file_record(timeline_path),
            "boundary_proposals": file_record(proposals_path),
            "visual_moments": file_record(visual_moments_path),
        }
        validation_config = {
            "boundary_validator": "validate-boundary-proposals",
            "visual_moment_validator": "validate-visual-moments",
        }
        validation_signature = stage_signature(
            "validate_evidence",
            validation_inputs,
            validation_config,
            implementation["digest"],
        )
        validation_dir = asset_root / "validation" / validation_signature[:16]
        validation_path = validation_dir / "validation.json"
        validation_commands = [
            [
                *cli_prefix(),
                "validate-boundary-proposals",
                str(proposals_path),
                str(timeline_path),
            ],
            [
                *cli_prefix(),
                "validate-visual-moments",
                str(visual_moments_path),
                str(timeline_path),
            ],
        ]

        def write_validation(command_results: list[dict[str, str]]) -> None:
            atomic_json(
                validation_path,
                {
                    "schema_version": VALIDATION_SCHEMA,
                    "status": "pass",
                    "validated_at": utc_now(),
                    "inputs": validation_inputs,
                    "validators": validation_config,
                    "command_results": command_results,
                },
            )

        statuses.append(
            execute_stage(
                asset_root=asset_root,
                state_path=state_path,
                state=state,
                name="validate_evidence",
                signature=validation_signature,
                inputs=validation_inputs,
                config=validation_config,
                commands=validation_commands,
                after_commands=write_validation,
                collect_outputs=lambda: {"validation": file_record(validation_path)},
                runner=runner,
            )
        )

        context_inputs = {
            "timeline_reviewed": file_record(timeline_path),
            "evidence_validation": file_record(validation_path),
        }
        context_config = {"max_frames": config.max_frames}
        context_signature = stage_signature(
            "context",
            context_inputs,
            context_config,
            implementation["digest"],
        )
        context_dir = asset_root / "context" / context_signature[:16]
        context_packet_path = context_dir / "context-review-packet.json"
        context_command = [
            *cli_prefix(),
            "build-context-packet",
            str(timeline_path),
            "--output-dir",
            str(context_dir),
            "--max-frames",
            str(config.max_frames),
        ]
        statuses.append(
            execute_stage(
                asset_root=asset_root,
                state_path=state_path,
                state=state,
                name="context",
                signature=context_signature,
                inputs=context_inputs,
                config=context_config,
                commands=[context_command],
                collect_outputs=lambda: {
                    "context_packet": file_record(context_packet_path),
                    "storyboards": directory_record(context_dir / "storyboards"),
                },
                runner=runner,
            )
        )

        packet_inputs = {
            "apple_transcript": file_record(apple_path),
            "timeline_reviewed": file_record(timeline_path),
            "context_packet": file_record(context_packet_path),
            "boundary_proposals": file_record(proposals_path),
            "visual_moments": file_record(visual_moments_path),
            "evidence_validation": file_record(validation_path),
        }
        packet_config = {"max_window": config.max_window, "clip_evidence": config.clip_evidence}
        packet_signature = stage_signature(
            "scene_dialogue_packet",
            packet_inputs,
            packet_config,
            implementation["digest"],
        )
        packet_dir = asset_root / "scene-dialogue" / packet_signature[:16]
        packet_path = packet_dir / "review-packet.json"
        packet_command = [
            *cli_prefix(),
            "build-scene-dialogue-review-packet",
            str(apple_path),
            str(timeline_path),
            "--visual-packet",
            str(context_packet_path),
            "--boundary-proposals",
            str(proposals_path),
            "--visual-moments",
            str(visual_moments_path),
            "--max-window",
            str(config.max_window),
            "--output",
            str(packet_path),
        ]
        if config.clip_evidence:
            packet_command.append("--clip-evidence")
        statuses.append(
            execute_stage(
                asset_root=asset_root,
                state_path=state_path,
                state=state,
                name="scene_dialogue_packet",
                signature=packet_signature,
                inputs=packet_inputs,
                config=packet_config,
                commands=[packet_command],
                collect_outputs=lambda: {"review_packet": file_record(packet_path)},
                runner=runner,
            )
        )
        state["status"] = "completed"
        state["completed_at"] = utc_now()
        state["current_outputs"] = {
            "boundary_proposals": str(proposals_path),
            "visual_moments": str(visual_moments_path),
            "context_packet": str(context_packet_path),
            "scene_dialogue_review_packet": str(packet_path),
        }
        update_state(state_path, state)
        return {
            "asset_id": asset_id,
            "source_relative_path": relative_path,
            "story_day": story_day,
            "status": "reused"
            if all(item == "reused" for item in statuses)
            else "completed",
            "grouping_origin": timeline_origin,
            "state": str(state_path),
            "outputs": state["current_outputs"],
            "stage_statuses": {
                name: state["stages"][name]["status"]
                for name in (
                    "boundaries",
                    "validate_evidence",
                    "context",
                    "scene_dialogue_packet",
                )
            },
        }
    except StageLockedError as error:
        return {
            "asset_id": asset_id,
            "source_relative_path": relative_path,
            "story_day": story_day,
            "status": "locked",
            "error": {"type": type(error).__name__, "message": str(error)},
            "state": str(state_path),
        }
    except Exception as error:
        if state_path.parent.exists():
            try:
                current = load_json(state_path) if state_path.is_file() else {}
                current.update(
                    {
                        "schema_version": STATE_SCHEMA,
                        "asset_id": asset_id,
                        "source_relative_path": relative_path,
                        "story_day": story_day,
                        "status": "failed",
                        "error": {
                            "type": type(error).__name__,
                            "message": error_text(error),
                        },
                    }
                )
                update_state(state_path, current)
            except Exception:
                pass
        return {
            "asset_id": asset_id,
            "source_relative_path": relative_path,
            "story_day": story_day,
            "status": "failed",
            "error": {"type": type(error).__name__, "message": error_text(error)},
            "state": str(state_path),
        }


def prepare_batch(
    story_days_path: Path,
    output_root: Path,
    *,
    requested_days: list[str] | None = None,
    excluded_days: list[str] | None = None,
    asset_regex: str | None = None,
    grouping_overrides_path: Path | None = None,
    jobs: int = 1,
    config: GoldenV3Config | None = None,
    runner: Runner = run_cli,
) -> dict[str, Any]:
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    config = config or GoldenV3Config()
    config.validate()
    story_days_path = story_days_path.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    manifest = load_json(story_days_path)
    if manifest.get("schema_version") not in STORY_DAYS_SCHEMAS:
        raise ValueError(
            "Expected story-day schema in "
            f"{sorted(STORY_DAYS_SCHEMAS)}, got {manifest.get('schema_version')!r}"
        )
    output_root_is_safe(manifest, output_root)
    story_days = select_days(manifest, requested_days or [], excluded_days or [])
    assets = select_assets(manifest, story_days, asset_regex)
    validate_unique_asset_ids(assets)
    overrides, override_manifest = load_grouping_overrides(grouping_overrides_path)
    unknown_overrides = sorted(
        set(overrides)
        - {str(item["source_relative_path"]) for item in manifest["assets"]}
    )
    if unknown_overrides:
        raise ValueError(
            f"Grouping overrides reference unknown assets: {unknown_overrides}"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    implementation = implementation_record()
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {
            executor.submit(
                prepare_asset,
                asset,
                output_root,
                overrides,
                override_manifest,
                implementation,
                config,
                runner,
            ): asset
            for asset in assets
        }
        for future in as_completed(futures):
            records.append(future.result())
    records.sort(
        key=lambda item: (str(item["story_day"]), str(item["source_relative_path"]))
    )
    statuses = ("completed", "reused", "blocked", "excluded", "locked", "failed")
    summary = {
        "selected_asset_count": len(records),
        **{
            status: sum(item["status"] == status for item in records)
            for status in statuses
        },
    }
    day_metadata = {str(item["story_day"]): item for item in manifest["story_days"]}
    day_manifests: dict[str, str] = {}
    for story_day in story_days:
        day_records = [item for item in records if item["story_day"] == story_day]
        day_path = output_root / "days" / story_day / "manifest.json"
        atomic_json(
            day_path,
            {
                "schema_version": DAY_MANIFEST_SCHEMA,
                "generated_at": utc_now(),
                "story_day": story_day,
                "label": day_metadata[story_day].get("label"),
                "source_manifest": file_record(story_days_path),
                "prepare_only": True,
                "summary": {
                    "selected_asset_count": len(day_records),
                    **{
                        status: sum(item["status"] == status for item in day_records)
                        for status in statuses
                    },
                },
                "records": day_records,
            },
        )
        day_manifests[story_day] = str(day_path)
    failures = [
        item for item in records if item["status"] in {"failed", "blocked", "locked"}
    ]
    errors_path = output_root / "errors.json"
    atomic_json(
        errors_path,
        {
            "schema_version": "golden-v3-preparation-errors/v1",
            "generated_at": utc_now(),
            "count": len(failures),
            "records": failures,
        },
    )
    result = {
        "schema_version": MANIFEST_SCHEMA,
        "generated_at": utc_now(),
        "prepare_only": True,
        "story_day_manifest": file_record(story_days_path),
        "grouping_override_manifest": override_manifest,
        "output_root": str(output_root),
        "selection": {
            "story_days": story_days,
            "excluded_story_days": excluded_days or [],
            "asset_regex": asset_regex,
            "jobs": jobs,
        },
        "implementation": implementation,
        "config": asdict(config),
        "config_digest": canonical_digest(asdict(config)),
        "policy": {
            "preflight_ineligible_assets_are_excluded": True,
            "accepted_quick_grouping_required": True,
            "model_review_performed": False,
            "review_merge_performed": False,
            "media_modified": False,
            "generated_media": "analysis-only visual-moment frames and context storyboards",
        },
        "summary": summary,
        "records": records,
        "day_manifests": day_manifests,
        "errors": str(errors_path),
    }
    atomic_json(output_root / "manifest.json", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare Golden v3 boundary, visual-moment, context, and integrated "
            "scene-dialogue packets without model review or rendering"
        )
    )
    parser.add_argument(
        "story_days",
        type=Path,
        help="travel-video-story-day-preflight/v1 JSON",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--story-day",
        action="append",
        default=[],
        help="Story day to prepare; repeat for several days (default: all)",
    )
    parser.add_argument(
        "--exclude-story-day",
        action="append",
        default=[],
        help="Story day to omit; repeat as needed",
    )
    parser.add_argument(
        "--asset-regex",
        help="Optional pilot regex matched against source_relative_path",
    )
    parser.add_argument(
        "--grouping-overrides",
        type=Path,
        help=(
            "Optional golden-v3-grouping-overrides/v1 JSON; every used entry "
            "must set accepted=true"
        ),
    )
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--max-frames", type=int, default=16)
    parser.add_argument("--max-window", type=float, default=8.0)
    parser.add_argument("--clip-evidence", action="store_true")
    parser.add_argument("--vision-interval", type=float, default=1 / 3)
    parser.add_argument("--ocr-interval", type=float, default=1.0)
    parser.add_argument("--motion-interval", type=float, default=1 / 3)
    parser.add_argument("--ffmpeg-scene-threshold", type=float, default=0.30)
    parser.add_argument("--cluster-tolerance", type=float, default=0.55)
    parser.add_argument("--neighbor-context", type=float, default=5.0)
    parser.add_argument("--feature-distance-floor", type=float, default=0.12)
    parser.add_argument("--feature-distance-quantile", type=float, default=0.90)
    parser.add_argument("--motion-delta-quantile", type=float, default=0.92)
    parser.add_argument("--visual-moment-window", type=float, default=6.0)
    parser.add_argument("--visual-moment-nms", type=float, default=3.0)
    parser.add_argument("--visual-moments-per-minute", type=float, default=6.0)
    parser.add_argument("--visual-moment-thumbnail-width", type=int, default=960)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = GoldenV3Config(
        clip_evidence=args.clip_evidence,
        max_frames=args.max_frames,
        max_window=args.max_window,
        vision_interval=args.vision_interval,
        ocr_interval=args.ocr_interval,
        motion_interval=args.motion_interval,
        ffmpeg_scene_threshold=args.ffmpeg_scene_threshold,
        cluster_tolerance=args.cluster_tolerance,
        neighbor_context=args.neighbor_context,
        feature_distance_floor=args.feature_distance_floor,
        feature_distance_quantile=args.feature_distance_quantile,
        motion_delta_quantile=args.motion_delta_quantile,
        visual_moment_window=args.visual_moment_window,
        visual_moment_nms=args.visual_moment_nms,
        visual_moments_per_minute=args.visual_moments_per_minute,
        visual_moment_thumbnail_width=args.visual_moment_thumbnail_width,
    )
    try:
        manifest = prepare_batch(
            args.story_days,
            args.output_root,
            requested_days=args.story_day,
            excluded_days=args.exclude_story_day,
            asset_regex=args.asset_regex,
            grouping_overrides_path=args.grouping_overrides,
            jobs=args.jobs,
            config=config,
        )
    except Exception as error:  # noqa: BLE001 - CLI boundary maps failures to status.
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    incomplete = sum(
        manifest["summary"][status] for status in ("failed", "blocked", "locked")
    )
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
