#!/usr/bin/env python3
"""Prepare fresh integrated scene-dialogue review packets from an inventory.

The Golden path starts from the quick grouped ``timeline.reviewed.json``, builds
a fresh dense visual packet, and combines it with raw Apple evidence. The
legacy context-reviewed input remains available for old inventories. This
batch stops before model review, validation, or merge; prior reconciliation,
review, summary, and final-timeline derivatives are never command inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


INVENTORY_SCHEMA = "unified-dialogue-inventory/v1"
MANIFEST_SCHEMA = "scene-dialogue-batch-manifest/v1"
ERRORS_SCHEMA = "scene-dialogue-batch-errors/v1"
STATE_SCHEMA = "scene-dialogue-batch-asset-state/v1"
SAFE_ASSET_ID = re.compile(r"^[A-Za-z0-9._-]+$")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CliRunner = Callable[[list[str], Path], dict[str, str]]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_shard(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", value)
    if not match:
        raise ValueError("--shard must use one-based INDEX/TOTAL syntax, for example 1/3")
    index, total = int(match.group(1)), int(match.group(2))
    if index > total:
        raise ValueError("--shard INDEX cannot exceed TOTAL")
    return index, total


def cli_command() -> list[str]:
    return [sys.executable, "-m", "travel_video.cli"]


def run_cli(command: list[str], cwd: Path) -> dict[str, str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return {"stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}


def selected_assets(
    inventory: dict[str, Any], asset_regex: str | None, shard: tuple[int, int] | None
) -> list[dict[str, Any]]:
    if inventory.get("schema_version") != INVENTORY_SCHEMA:
        raise ValueError(f"Expected inventory schema {INVENTORY_SCHEMA}")
    assets = inventory.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Inventory assets must be a list")
    pattern = re.compile(asset_regex) if asset_regex else None
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in assets:
        if not isinstance(raw, dict):
            raise ValueError("Every inventory asset must be an object")
        asset_id = str(raw.get("asset_id") or "")
        source_relative_path = str(raw.get("source_relative_path") or "")
        if not asset_id or not SAFE_ASSET_ID.fullmatch(asset_id):
            raise ValueError(f"Unsafe or missing asset_id: {asset_id!r}")
        if asset_id in seen_ids:
            raise ValueError(f"Duplicate asset_id: {asset_id}")
        seen_ids.add(asset_id)
        if pattern and not (
            pattern.search(asset_id) or pattern.search(source_relative_path)
        ):
            continue
        selected.append(raw)
    selected.sort(key=lambda item: str(item["asset_id"]))
    if shard:
        index, total = shard
        selected = [
            asset for offset, asset in enumerate(selected) if offset % total == index - 1
        ]
    return selected


def canonical_inputs(asset: dict[str, Any]) -> tuple[Path, Path, bool]:
    asset_id = str(asset["asset_id"])
    apple_value = asset.get("apple_transcript_path")
    grouped_value = asset.get("timeline_reviewed_path")
    timeline_value = grouped_value or asset.get("timeline_context_reviewed_path")
    if not apple_value or not timeline_value:
        raise ValueError(
            f"{asset_id}: apple_transcript_path and either timeline_reviewed_path "
            "or timeline_context_reviewed_path are required"
        )
    apple_path = Path(str(apple_value)).expanduser().resolve()
    timeline_path = Path(str(timeline_value)).expanduser().resolve()
    if apple_path.name != "transcript.apple.json":
        raise ValueError(
            f"{asset_id}: apple_transcript_path must name transcript.apple.json"
        )
    integrated_visual = bool(grouped_value)
    expected_timeline_name = (
        "timeline.reviewed.json"
        if integrated_visual
        else "timeline.context-reviewed.json"
    )
    if timeline_path.name != expected_timeline_name:
        raise ValueError(
            f"{asset_id}: selected timeline path must name {expected_timeline_name}"
        )
    if not apple_path.is_file():
        raise FileNotFoundError(f"{asset_id}: missing raw Apple transcript: {apple_path}")
    if not timeline_path.is_file():
        raise FileNotFoundError(
            f"{asset_id}: missing context-reviewed timeline: {timeline_path}"
        )
    return apple_path, timeline_path, integrated_visual


def output_paths(output_root: Path, asset_id: str) -> dict[str, Path]:
    asset_root = output_root / asset_id
    return {
        "asset_root": asset_root,
        "scene_dialogue_packet": asset_root
        / "scene-dialogue"
        / "review-packet.json",
        "context_packet": asset_root / "context" / "context-review-packet.json",
        "state": asset_root / "state.json",
    }


def input_record(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def output_record(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def reusable_state(
    state_path: Path,
    inputs: dict[str, Any],
    paths: dict[str, Path],
    output_names: tuple[str, ...],
) -> dict[str, Any] | None:
    if not state_path.is_file():
        return None
    try:
        state = load_json(state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if state.get("schema_version") != STATE_SCHEMA or state.get("status") != "completed":
        return None
    if state.get("inputs") != inputs:
        return None
    outputs = state.get("outputs")
    if not isinstance(outputs, dict):
        return None
    for name in output_names:
        path = paths[name]
        expected = outputs.get(name)
        if not path.is_file() or not isinstance(expected, dict):
            return None
        if expected.get("path") != str(path) or expected.get("sha256") != sha256_file(path):
            return None
    return state


def error_text(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        detail = (error.stderr or error.stdout or "").strip()
        if detail:
            return f"{error}: {detail}"
    return str(error)


def prepare_asset(
    asset: dict[str, Any],
    output_root: Path,
    runner: CliRunner,
    *,
    max_frames: int,
    max_window: float,
) -> dict[str, Any]:
    asset_id = str(asset["asset_id"])
    paths = output_paths(output_root, asset_id)
    started_at = utc_now()
    try:
        apple_path, timeline_path, integrated_visual = canonical_inputs(asset)
        timeline_key = (
            "timeline_reviewed"
            if integrated_visual
            else "timeline_context_reviewed"
        )
        inputs = {
            "apple_transcript": input_record(apple_path),
            timeline_key: input_record(timeline_path),
            "config": {
                "integrated_visual": integrated_visual,
                "max_frames": max_frames if integrated_visual else None,
                "max_window": max_window,
            },
        }
        output_names = (
            ("context_packet", "scene_dialogue_packet")
            if integrated_visual
            else ("scene_dialogue_packet",)
        )
        reused = reusable_state(paths["state"], inputs, paths, output_names)
        if reused is not None:
            return {
                "asset_id": asset_id,
                "source_relative_path": asset.get("source_relative_path"),
                "status": "reused",
                "inputs": inputs,
                "outputs": reused["outputs"],
            }

        if integrated_visual:
            context_command = [
                *cli_command(),
                "build-context-packet",
                str(timeline_path),
                "--output-dir",
                str(paths["context_packet"].parent),
                "--max-frames",
                str(max_frames),
            ]
            runner(context_command, PROJECT_ROOT)
            if not paths["context_packet"].is_file():
                raise RuntimeError("Context packet command produced no output")

        paths["scene_dialogue_packet"].parent.mkdir(parents=True, exist_ok=True)
        scene_dialogue_command = [
            *cli_command(),
            "build-scene-dialogue-review-packet",
            str(apple_path),
            str(timeline_path),
            "--max-window",
            str(max_window),
            "--output",
            str(paths["scene_dialogue_packet"]),
        ]
        if integrated_visual:
            scene_dialogue_command.extend(
                ["--visual-packet", str(paths["context_packet"])]
            )
        runner(scene_dialogue_command, PROJECT_ROOT)
        if not paths["scene_dialogue_packet"].is_file():
            raise RuntimeError("Scene-dialogue packet command produced no output")
        outputs: dict[str, Any] = {
            "scene_dialogue_packet": output_record(paths["scene_dialogue_packet"]),
        }
        if integrated_visual:
            outputs["context_packet"] = output_record(paths["context_packet"])
        state = {
            "schema_version": STATE_SCHEMA,
            "asset_id": asset_id,
            "status": "completed",
            "started_at": started_at,
            "completed_at": utc_now(),
            "inputs": inputs,
            "outputs": outputs,
            "policy": {
                "fresh_reconciliation_built_inside_scene_packet": True,
                "integrated_visual_packet": integrated_visual,
                "max_frames": max_frames if integrated_visual else None,
                "max_window": max_window,
                "model_review_performed": False,
                "merge_performed": False,
                "media_written": False,
            },
        }
        atomic_json(paths["state"], state)
        return {
            "asset_id": asset_id,
            "source_relative_path": asset.get("source_relative_path"),
            "status": "completed",
            "inputs": inputs,
            "outputs": outputs,
        }
    except Exception as error:  # noqa: BLE001 - per-asset errors belong in batch JSON.
        failed = {
            "schema_version": STATE_SCHEMA,
            "asset_id": asset_id,
            "status": "failed",
            "started_at": started_at,
            "completed_at": utc_now(),
            "error": {
                "type": type(error).__name__,
                "message": error_text(error),
            },
        }
        atomic_json(paths["state"], failed)
        return {
            "asset_id": asset_id,
            "source_relative_path": asset.get("source_relative_path"),
            "status": "failed",
            "error": failed["error"],
            "state": str(paths["state"]),
        }


def build_batch(
    inventory_path: Path,
    output_root: Path,
    *,
    asset_regex: str | None = None,
    shard: str | None = None,
    jobs: int = 1,
    max_frames: int = 16,
    max_window: float = 8.0,
    runner: CliRunner = run_cli,
) -> dict[str, Any]:
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    if max_frames < 1:
        raise ValueError("max_frames must be at least 1")
    if max_window <= 0:
        raise ValueError("max_window must be positive")
    inventory_path = inventory_path.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    inventory = load_json(inventory_path)
    parsed_shard = parse_shard(shard)
    assets = selected_assets(inventory, asset_regex, parsed_shard)
    output_root.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {
            executor.submit(
                prepare_asset,
                asset,
                output_root,
                runner,
                max_frames=max_frames,
                max_window=max_window,
            ): asset
            for asset in assets
        }
        for future in as_completed(futures):
            records.append(future.result())
    records.sort(key=lambda item: str(item["asset_id"]))
    errors = [
        {
            "asset_id": item["asset_id"],
            "source_relative_path": item.get("source_relative_path"),
            "error": item["error"],
            "state": item["state"],
        }
        for item in records
        if item["status"] == "failed"
    ]
    error_report = {
        "schema_version": ERRORS_SCHEMA,
        "generated_at": utc_now(),
        "inventory": str(inventory_path),
        "error_count": len(errors),
        "errors": errors,
    }
    atomic_json(output_root / "errors.json", error_report)

    status_counts = {
        name: sum(item["status"] == name for item in records)
        for name in ("completed", "reused", "failed")
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "generated_at": utc_now(),
        "inventory": {
            "path": str(inventory_path),
            "sha256": sha256_file(inventory_path),
            "schema_version": inventory["schema_version"],
        },
        "output_root": str(output_root),
        "selection": {
            "asset_regex": asset_regex,
            "shard": shard,
            "jobs": jobs,
            "max_frames": max_frames,
            "max_window": max_window,
            "inventory_asset_count": len(inventory["assets"]),
            "selected_asset_count": len(assets),
        },
        "policy": {
            "inputs": [
                "apple_transcript_path",
                "timeline_reviewed_path (Golden integrated path) or "
                "timeline_context_reviewed_path (legacy compatibility)",
            ],
            "ignored_prior_derivatives": [
                "existing_reconciliation_packet_path",
                "existing_reconciliation_review_path",
                "existing_reconciled_transcript_path",
                "timeline_final_prior_pipeline_path",
            ],
            "fresh_reconciliation_built_inside_scene_packet": True,
            "fresh_dense_visual_packet_for_grouped_timelines": True,
            "model_review_performed": False,
            "merge_performed": False,
            "media_written": False,
        },
        "summary": {"asset_count": len(records), **status_counts},
        "records": records,
        "errors": str(output_root / "errors.json"),
    }
    atomic_json(output_root / "manifest.json", manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build scene-dialogue packets with fresh internal reconciliation"
    )
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--asset-regex",
        help="Regex matched against asset_id or source_relative_path",
    )
    parser.add_argument(
        "--shard",
        help="Deterministic one-based asset shard, for example 1/3",
    )
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument(
        "--max-frames",
        type=int,
        default=16,
        help="Maximum dense storyboard frames per coarse group",
    )
    parser.add_argument(
        "--max-window",
        type=float,
        default=8.0,
        help="Maximum dialogue review window before atomic-span expansion",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = build_batch(
            args.inventory,
            args.output_root,
            asset_regex=args.asset_regex,
            shard=args.shard,
            jobs=args.jobs,
            max_frames=args.max_frames,
            max_window=args.max_window,
        )
    except Exception as error:  # noqa: BLE001 - CLI boundary converts to exit code.
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 1 if manifest["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
