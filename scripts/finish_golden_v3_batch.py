#!/usr/bin/env python3
"""Finish Golden v3 assets after model-authored scene review.

The script consumes a Golden v3 preparation root/day manifest and a declared
review root.  It validates and merges model-authored JSON but never writes or
repairs model content.  Assets without a review or whole-video summary stop in
an explicit awaiting state and can be resumed after the missing JSON appears.

Review-root convention::

    <review-root>/<asset_id>/scene-dialogue/review.json
    <review-root>/<asset_id>/summary/video-summary.json  # optional initially

All deterministic outputs live below ``--output-root``.  Source and proxy media
are read-only; the finisher writes only JSON, state, HTML, and relative links.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from run_golden_v3_batch import (
    PROJECT_ROOT,
    SAFE_ASSET_ID,
    StageLockedError,
    atomic_json,
    canonical_digest,
    directory_record,
    error_text,
    file_record,
    is_relative_to,
    load_json,
    nonblocking_lock,
    normalized_path,
    record_is_current,
    run_cli,
    utc_now,
)


PREPARATION_MANIFEST_SCHEMA = "golden-v3-preparation-manifest/v1"
PREPARATION_DAY_MANIFEST_SCHEMA = "golden-v3-preparation-day-manifest/v1"
PREPARATION_STATE_SCHEMA = "golden-v3-preparation-asset-state/v1"
FINISHER_STATE_SCHEMA = "golden-v3-finisher-asset-state/v1"
FINISHER_MANIFEST_SCHEMA = "golden-v3-finisher-manifest/v1"
FINISHER_DAY_MANIFEST_SCHEMA = "golden-v3-finisher-day-manifest/v1"
SCENE_AUDIT_SCHEMA = "golden-v3-scene-dialogue-audit/v1"
DIALOGUE_POLICY = "dialogue-preservation/v1"
STORY_DAY_MANIFEST_SCHEMAS = {
    "travel-video-story-day-preflight/v1",
    "travel-video-story-day-preflight/v2",
}
Runner = Callable[[list[str], Path], dict[str, str]]


@dataclass(frozen=True)
class PreparedAsset:
    asset_id: str
    source_relative_path: str
    story_day: str
    preparation_state_path: Path
    preparation_state: dict[str, Any]
    timeline_path: Path
    timeline_record: dict[str, Any]
    packet_path: Path
    packet_record: dict[str, Any]
    timeline: dict[str, Any]
    packet: dict[str, Any]


def cli_prefix() -> list[str]:
    return [sys.executable, "-m", "travel_video.cli"]


def implementation_record() -> dict[str, Any]:
    candidates = [
        Path(__file__).resolve(),
        PROJECT_ROOT / "scripts/run_golden_v3_batch.py",
        PROJECT_ROOT / "src/travel_video/cli.py",
        PROJECT_ROOT / "src/travel_video/scene_dialogue.py",
        PROJECT_ROOT / "src/travel_video/video_summary.py",
        PROJECT_ROOT / "src/travel_video/web.py",
    ]
    files: list[dict[str, Any]] = []
    for path in candidates:
        record = file_record(path)
        record["component"] = path.relative_to(PROJECT_ROOT).as_posix()
        files.append(record)
    return {
        "digest": canonical_digest(
            [
                {"component": item["component"], "sha256": item["sha256"]}
                for item in files
            ]
        ),
        "files": files,
    }


def preparation_source_manifest(
    preparation: dict[str, Any], preparation_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    schema = preparation.get("schema_version")
    if schema == PREPARATION_MANIFEST_SCHEMA:
        record = preparation.get("story_day_manifest")
    elif schema == PREPARATION_DAY_MANIFEST_SCHEMA:
        record = preparation.get("source_manifest")
    else:
        raise ValueError(
            "Expected a Golden v3 preparation root manifest or day manifest"
        )
    if not isinstance(record, dict) or not record_is_current(record):
        raise ValueError(
            f"Preparation manifest references a changed story-day source: {preparation_path}"
        )
    source = load_json(Path(str(record["path"])))
    if source.get("schema_version") not in STORY_DAY_MANIFEST_SCHEMAS:
        raise ValueError("Preparation source is not a story-day preflight manifest")
    return source, record


def output_root_is_safe(source: dict[str, Any], output_root: Path) -> None:
    output_root = output_root.expanduser().resolve()
    for key in ("source_root", "proxy_root"):
        value = source.get(key)
        if value and is_relative_to(
            output_root, Path(str(value)).expanduser().resolve()
        ):
            raise ValueError(f"Output root must not be inside {key}: {output_root}")


def select_records(
    preparation: dict[str, Any], story_days: list[str], asset_regex: str | None
) -> list[dict[str, Any]]:
    records = preparation.get("records")
    if not isinstance(records, list):
        raise ValueError("Preparation records must be a list")
    available_days = sorted(
        {str(item.get("story_day") or "") for item in records if item.get("story_day")}
    )
    unknown = sorted(set(story_days) - set(available_days))
    if unknown:
        raise ValueError(f"Unknown story day(s): {', '.join(unknown)}")
    selected_days = set(story_days or available_days)
    pattern = re.compile(asset_regex) if asset_regex else None
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in records:
        if not isinstance(raw, dict):
            raise ValueError("Every preparation record must be an object")
        story_day = str(raw.get("story_day") or "")
        if story_day not in selected_days:
            continue
        asset_id = str(raw.get("asset_id") or "")
        source_relative_path = str(raw.get("source_relative_path") or "")
        if not asset_id or not SAFE_ASSET_ID.fullmatch(asset_id):
            raise ValueError(f"Unsafe preparation asset_id: {asset_id!r}")
        if asset_id in seen_ids:
            raise ValueError(f"Duplicate preparation asset_id: {asset_id}")
        seen_ids.add(asset_id)
        if pattern and not (
            pattern.search(asset_id) or pattern.search(source_relative_path)
        ):
            continue
        selected.append(raw)
    selected.sort(
        key=lambda item: (str(item["story_day"]), str(item["source_relative_path"]))
    )
    return selected


def _require_current_record(
    value: Any, label: str, *, expected_path: Path | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict) or not record_is_current(value):
        raise ValueError(f"Prepared {label} is missing or its content hash changed")
    if expected_path is not None and normalized_path(value["path"]) != normalized_path(
        expected_path
    ):
        raise ValueError(f"Prepared {label} path does not match current output")
    return value


def verify_prepared_asset(record: dict[str, Any]) -> PreparedAsset:
    asset_id = str(record["asset_id"])
    state_value = record.get("state")
    if not state_value:
        raise ValueError("Completed preparation record has no state path")
    state_path = Path(str(state_value)).expanduser().resolve()
    state = load_json(state_path)
    if state.get("schema_version") != PREPARATION_STATE_SCHEMA:
        raise ValueError("Preparation state schema is incompatible")
    if state.get("status") != "completed":
        raise ValueError("Preparation state is not completed")
    for field in ("asset_id", "source_relative_path", "story_day"):
        if state.get(field) != record.get(field):
            raise ValueError(f"Preparation record/state {field} mismatch")

    current_outputs = state.get("current_outputs")
    stages = state.get("stages")
    inputs = state.get("inputs")
    if not all(isinstance(item, dict) for item in (current_outputs, stages, inputs)):
        raise ValueError("Preparation state is missing inputs, stages, or outputs")
    packet_stage = stages.get("scene_dialogue_packet")
    if not isinstance(packet_stage, dict) or packet_stage.get("status") not in {
        "completed",
        "reused",
    }:
        raise ValueError("Preparation scene-dialogue packet stage is incomplete")
    packet_stage_inputs = packet_stage.get("inputs")
    packet_stage_outputs = packet_stage.get("outputs")
    if not isinstance(packet_stage_inputs, dict) or not isinstance(
        packet_stage_outputs, dict
    ):
        raise ValueError("Preparation packet stage has no hashed inputs/outputs")

    packet_path = Path(str(current_outputs.get("scene_dialogue_review_packet")))
    timeline_path = Path(str(inputs.get("timeline_reviewed", {}).get("path")))
    packet_record = _require_current_record(
        packet_stage_outputs.get("review_packet"),
        "scene-dialogue packet",
        expected_path=packet_path,
    )
    timeline_record = _require_current_record(
        inputs.get("timeline_reviewed"),
        "reviewed timeline",
        expected_path=timeline_path,
    )
    required_packet_inputs = {
        "apple_transcript",
        "timeline_reviewed",
        "context_packet",
        "boundary_proposals",
        "visual_moments",
        "evidence_validation",
    }
    if set(packet_stage_inputs) != required_packet_inputs:
        raise ValueError(
            "Preparation packet stage does not have exact Golden v3 inputs"
        )
    for key in sorted(required_packet_inputs):
        _require_current_record(packet_stage_inputs.get(key), f"packet input {key}")
    packet_timeline_record = packet_stage_inputs["timeline_reviewed"]
    if normalized_path(packet_timeline_record["path"]) != normalized_path(
        timeline_record["path"]
    ) or packet_timeline_record.get("sha256") != timeline_record.get("sha256"):
        raise ValueError(
            "Preparation state and packet stage disagree on reviewed timeline hash"
        )

    record_outputs = record.get("outputs")
    if not isinstance(record_outputs, dict):
        raise ValueError("Preparation manifest record has no output paths")
    if normalized_path(record_outputs.get("scene_dialogue_review_packet", "")) != (
        normalized_path(packet_path)
    ):
        raise ValueError("Preparation manifest packet path differs from asset state")

    timeline = load_json(timeline_path)
    packet = load_json(packet_path)
    if timeline.get("asset_id") != asset_id or packet.get("asset_id") != asset_id:
        raise ValueError("Prepared packet/timeline asset identity mismatch")
    if timeline.get("source") != packet.get("source"):
        raise ValueError("Prepared packet/timeline source identity mismatch")
    timeline_duration = float(timeline.get("media", {}).get("duration", -1))
    packet_duration = float(packet.get("media", {}).get("duration", -2))
    if abs(timeline_duration - packet_duration) > 1e-6:
        raise ValueError("Prepared packet/timeline duration mismatch")
    packet_inputs = packet.get("inputs")
    if not isinstance(packet_inputs, dict):
        raise ValueError("Prepared scene-dialogue packet has no input paths")
    expected_packet_paths = {
        "timeline": packet_stage_inputs["timeline_reviewed"]["path"],
        "visual_packet": packet_stage_inputs["context_packet"]["path"],
        "boundary_proposals": packet_stage_inputs["boundary_proposals"]["path"],
        "visual_moments": packet_stage_inputs["visual_moments"]["path"],
        "apple_transcript": packet_stage_inputs["apple_transcript"]["path"],
    }
    for key, expected in expected_packet_paths.items():
        if normalized_path(packet_inputs.get(key, "")) != normalized_path(expected):
            raise ValueError(
                f"Prepared packet {key} path differs from hashed stage input"
            )
    policy = packet.get("policy", {})
    required_policy = {
        "policy_version": DIALOGUE_POLICY,
        "canonical_for_editing": True,
        "integrated_visual_review": True,
        "editorial_beats_required": True,
        "boundary_proposals_supplied": True,
        "visual_moments_supplied": True,
    }
    for key, expected in required_policy.items():
        if policy.get(key) != expected:
            raise ValueError(f"Prepared packet policy {key} is not {expected!r}")
    return PreparedAsset(
        asset_id=asset_id,
        source_relative_path=str(record["source_relative_path"]),
        story_day=str(record["story_day"]),
        preparation_state_path=state_path,
        preparation_state=state,
        timeline_path=timeline_path,
        timeline_record=timeline_record,
        packet_path=packet_path,
        packet_record=packet_record,
        timeline=timeline,
        packet=packet,
    )


def visual_assignment_audit(
    packet: dict[str, Any], review: dict[str, Any], merged: dict[str, Any]
) -> dict[str, Any]:
    expected: dict[str, dict[str, str]] = {}
    for scene in packet.get("scenes", []):
        group_id = str(scene.get("group_id") or "")
        for moment in scene.get("visual_context", {}).get("visual_moments", []):
            moment_id = str(moment.get("moment_id") or "")
            if not moment_id or moment_id in expected:
                raise ValueError("Packet visual moment IDs are missing or duplicated")
            expected[moment_id] = {
                "group_id": group_id,
                "representative_sample_id": str(
                    moment.get("representative_sample_id") or ""
                ),
            }
            if not expected[moment_id]["representative_sample_id"]:
                raise ValueError(
                    f"Packet visual moment {moment_id} has no representative sample"
                )
    review_assignments: list[str] = []
    review_samples: dict[str, set[str]] = {}
    for scene in review.get("scenes", []):
        group_id = str(scene.get("group_id") or "")
        for beat in scene.get("editorial_beats", []):
            sample_ids = {str(item) for item in beat.get("source_sample_ids", [])}
            for moment_id in beat.get("source_visual_moment_ids", []):
                moment_id = str(moment_id)
                if (
                    moment_id in expected
                    and expected[moment_id]["group_id"] != group_id
                ):
                    raise ValueError(
                        f"Visual moment {moment_id} was assigned to the wrong scene"
                    )
                review_assignments.append(moment_id)
                review_samples.setdefault(moment_id, set()).update(sample_ids)
    merged_assignments: list[str] = []
    for beat in merged.get("reviewed_dialogue", {}).get("editorial_beats", []):
        group_id = str(beat.get("group_id") or "")
        for moment_id in beat.get("source_visual_moment_ids", []):
            moment_id = str(moment_id)
            if moment_id in expected and expected[moment_id]["group_id"] != group_id:
                raise ValueError(
                    f"Merged visual moment {moment_id} belongs to the wrong scene"
                )
            merged_assignments.append(moment_id)
    expected_counter = Counter({item: 1 for item in expected})
    review_counter = Counter(review_assignments)
    merged_counter = Counter(merged_assignments)
    if review_counter != expected_counter:
        raise ValueError("Model review must assign every visual moment exactly once")
    if merged_counter != expected_counter:
        raise ValueError(
            "Merged timeline did not preserve exact visual-moment assignment"
        )
    missing_representatives = [
        moment_id
        for moment_id, metadata in expected.items()
        if metadata["representative_sample_id"]
        not in review_samples.get(moment_id, set())
    ]
    if missing_representatives:
        raise ValueError(
            "Visual-moment beats omit representative samples: "
            + ", ".join(missing_representatives)
        )
    packet_count = int(packet.get("summary", {}).get("visual_moment_count", 0))
    if packet_count != len(expected):
        raise ValueError("Packet visual-moment summary count is inconsistent")
    available = packet.get("policy", {}).get("visual_moments_available")
    if bool(expected) != bool(available):
        raise ValueError("Packet visual-moment availability is inconsistent")
    return {
        "status": "pass",
        "expected_count": len(expected),
        "review_assignment_count": len(review_assignments),
        "merged_assignment_count": len(merged_assignments),
        "moment_ids": sorted(expected),
    }


def audit_merged_timeline(
    prepared: PreparedAsset, review_path: Path, merged_path: Path
) -> dict[str, Any]:
    review = load_json(review_path)
    merged = load_json(merged_path)
    if merged.get("schema_version") != "phase1-context-reviewed-timeline/v1":
        raise ValueError(
            "Scene-dialogue merge did not produce a context-reviewed timeline"
        )
    if merged.get("asset_id") != prepared.asset_id:
        raise ValueError("Merged timeline asset_id differs from prepared packet")
    if merged.get("source") != prepared.timeline.get("source"):
        raise ValueError("Merged timeline changed source identity")
    if merged.get("media") != prepared.timeline.get("media"):
        raise ValueError("Merged timeline changed media identity")
    reviewed = merged.get("reviewed_dialogue")
    if not isinstance(reviewed, dict):
        raise ValueError("Merged timeline has no reviewed_dialogue")
    policy = reviewed.get("policy", {})
    policy_audit = reviewed.get("policy_audit", {})
    if policy.get("policy_version") != DIALOGUE_POLICY:
        raise ValueError("Merged timeline does not use dialogue-preservation/v1")
    if policy_audit.get("status") != "pass":
        raise ValueError("Merged dialogue policy audit did not pass")
    if float(policy_audit.get("caption_coverage_ratio", -1)) != 1.0:
        raise ValueError("Merged caption coverage ratio is not exactly 1.0")
    if policy_audit.get("uncaptioned_lexical_utterance_ids"):
        raise ValueError("Merged timeline has uncaptioned lexical utterances")
    visual_audit = visual_assignment_audit(prepared.packet, review, merged)
    return {
        "schema_version": SCENE_AUDIT_SCHEMA,
        "status": "pass",
        "asset_id": prepared.asset_id,
        "generated_at": utc_now(),
        "inputs": {
            "prepared_packet": file_record(prepared.packet_path),
            "prepared_timeline": file_record(prepared.timeline_path),
            "model_review": file_record(review_path),
            "merged_timeline": file_record(merged_path),
        },
        "dialogue_policy": {
            "policy_version": policy["policy_version"],
            "status": policy_audit["status"],
            "caption_coverage_ratio": policy_audit["caption_coverage_ratio"],
            "uncaptioned_lexical_utterance_ids": policy_audit.get(
                "uncaptioned_lexical_utterance_ids", []
            ),
        },
        "visual_moment_assignment": visual_audit,
    }


def verify_summary_packet(
    prepared: PreparedAsset, merged_path: Path, summary_packet_path: Path
) -> None:
    packet = load_json(summary_packet_path)
    if packet.get("schema_version") != "phase1-video-summary-packet/v1":
        raise ValueError("Whole-video summary packet schema is invalid")
    if packet.get("asset_id") != prepared.asset_id:
        raise ValueError("Whole-video summary packet asset_id differs")
    if packet.get("source") != prepared.timeline.get("source"):
        raise ValueError("Whole-video summary packet changed source identity")
    if normalized_path(packet.get("timeline", "")) != normalized_path(merged_path):
        raise ValueError("Whole-video summary packet points to a different timeline")


def verify_summarized_timeline(
    prepared: PreparedAsset,
    summary_review_path: Path,
    summarized_path: Path,
) -> None:
    summarized = load_json(summarized_path)
    summary_review = load_json(summary_review_path)
    if summarized.get("schema_version") != "phase1-video-summarized-timeline/v1":
        raise ValueError("Summary merge did not produce a summarized timeline")
    if summarized.get("asset_id") != prepared.asset_id:
        raise ValueError("Summarized timeline asset_id differs")
    if summarized.get("source") != prepared.timeline.get("source"):
        raise ValueError("Summarized timeline changed source identity")
    if summarized.get("video_summary") != summary_review:
        raise ValueError("Summarized timeline does not preserve model summary exactly")


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
        isinstance(item, dict) and record_is_current(item) for item in outputs.values()
    )


def update_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_json(path, state)


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
    runner: Runner,
    after_commands: Callable[[list[dict[str, str]]], None] | None,
    collect_outputs: Callable[[], dict[str, dict[str, Any]]],
) -> str:
    with nonblocking_lock(asset_root / "locks" / f"{name}.lock"):
        previous = state["stages"].get(name)
        if reusable_stage(previous, signature):
            state["stages"][name] = {
                **previous,
                "status": "reused",
                "last_reused_at": utc_now(),
            }
            update_state(state_path, state)
            return "reused"
        stage = {
            "stage_id": name,
            "status": "running",
            "signature": signature,
            "started_at": utc_now(),
            "inputs": inputs,
            "config": config,
            "config_digest": canonical_digest(config),
            "implementation_digest": state["implementation"]["digest"],
            "commands": commands,
        }
        state["stages"][name] = stage
        update_state(state_path, state)
        try:
            results = [runner(command, PROJECT_ROOT) for command in commands]
            if after_commands is not None:
                after_commands(results)
            outputs = collect_outputs()
            state["stages"][name] = {
                **stage,
                "status": "completed",
                "completed_at": utc_now(),
                "command_results": [
                    {
                        "stdout": result.get("stdout", "")[-4000:],
                        "stderr": result.get("stderr", "")[-4000:],
                    }
                    for result in results
                ],
                "outputs": outputs,
            }
            update_state(state_path, state)
            return "completed"
        except Exception as error:
            state["stages"][name] = {
                **stage,
                "status": "failed",
                "completed_at": utc_now(),
                "error": {
                    "type": type(error).__name__,
                    "message": error_text(error),
                },
            }
            state["status"] = "failed"
            update_state(state_path, state)
            raise


def _write_waiting_state(
    asset_root: Path,
    state_path: Path,
    state: dict[str, Any],
    *,
    status: str,
    expected_path: Path,
) -> None:
    with nonblocking_lock(asset_root / "locks/resolution.lock"):
        state["status"] = status
        state["expected_model_output"] = str(expected_path)
        update_state(state_path, state)


def finish_asset(
    record: dict[str, Any],
    review_root: Path,
    output_root: Path,
    implementation: dict[str, Any],
    runner: Runner,
    frame_limit: int,
) -> dict[str, Any]:
    asset_id = str(record["asset_id"])
    source_relative_path = str(record.get("source_relative_path") or "")
    story_day = str(record.get("story_day") or "")
    asset_root = output_root / "assets" / asset_id
    state_path = asset_root / "state.json"
    if record.get("status") not in {"completed", "reused"}:
        return {
            "asset_id": asset_id,
            "source_relative_path": source_relative_path,
            "story_day": story_day,
            "status": "skipped_preparation",
            "preparation_status": record.get("status"),
        }
    try:
        prepared = verify_prepared_asset(record)
        review_path = review_root / asset_id / "scene-dialogue" / "review.json"
        summary_review_path = review_root / asset_id / "summary" / "video-summary.json"
        with nonblocking_lock(asset_root / "locks/resolution.lock"):
            if state_path.is_file():
                state = load_json(state_path)
                if state.get("schema_version") != FINISHER_STATE_SCHEMA:
                    raise ValueError(f"Incompatible finisher state: {state_path}")
                if state.get("source_relative_path") != source_relative_path:
                    raise ValueError("Finisher state belongs to another source")
            else:
                state = {"schema_version": FINISHER_STATE_SCHEMA, "stages": {}}
            state.update(
                {
                    "asset_id": asset_id,
                    "source_relative_path": source_relative_path,
                    "story_day": story_day,
                    "status": "running",
                    "attempt_id": str(uuid.uuid4()),
                    "preparation_state": file_record(prepared.preparation_state_path),
                    "prepared_inputs": {
                        "timeline_reviewed": prepared.timeline_record,
                        "scene_dialogue_review_packet": prepared.packet_record,
                    },
                    "review_paths": {
                        "scene_dialogue_review": str(review_path),
                        "video_summary": str(summary_review_path),
                    },
                    "implementation": implementation,
                    "policy": {
                        "model_content_authored": False,
                        "source_or_proxy_media_modified": False,
                        "relative_asset_web": True,
                    },
                }
            )
            update_state(state_path, state)

        if not review_path.is_file():
            _write_waiting_state(
                asset_root,
                state_path,
                state,
                status="awaiting_review",
                expected_path=review_path,
            )
            return {
                "asset_id": asset_id,
                "source_relative_path": source_relative_path,
                "story_day": story_day,
                "status": "awaiting_review",
                "expected_review": str(review_path),
                "state": str(state_path),
            }

        review_inputs = {
            "preparation_identity": {
                "state_path": str(prepared.preparation_state_path),
                "run_signature": prepared.preparation_state.get("run_signature"),
                "schema_version": PREPARATION_STATE_SCHEMA,
            },
            "timeline_reviewed": file_record(prepared.timeline_path),
            "scene_dialogue_review_packet": file_record(prepared.packet_path),
            "model_scene_dialogue_review": file_record(review_path),
        }
        review_config = {
            "policy_version": DIALOGUE_POLICY,
            "caption_coverage_ratio": 1.0,
            "visual_moment_assignment": "exactly_once_with_representative_sample",
        }
        review_signature = stage_signature(
            "review_merge",
            review_inputs,
            review_config,
            implementation["digest"],
        )
        review_output_dir = asset_root / "scene-dialogue" / review_signature[:16]
        merged_path = review_output_dir / "timeline.dialogue-reviewed.json"
        audit_path = review_output_dir / "audit.json"
        review_commands = [
            [
                *cli_prefix(),
                "validate-scene-dialogue-review",
                str(prepared.packet_path),
                str(review_path),
            ],
            [
                *cli_prefix(),
                "merge-scene-dialogue-review",
                str(prepared.timeline_path),
                str(prepared.packet_path),
                str(review_path),
                "--output",
                str(merged_path),
            ],
        ]

        def after_review_merge(_: list[dict[str, str]]) -> None:
            atomic_json(
                audit_path,
                audit_merged_timeline(prepared, review_path, merged_path),
            )

        statuses: list[str] = []
        statuses.append(
            execute_stage(
                asset_root=asset_root,
                state_path=state_path,
                state=state,
                name="review_merge",
                signature=review_signature,
                inputs=review_inputs,
                config=review_config,
                commands=review_commands,
                runner=runner,
                after_commands=after_review_merge,
                collect_outputs=lambda: {
                    "timeline_dialogue_reviewed": file_record(merged_path),
                    "audit": file_record(audit_path),
                },
            )
        )

        summary_packet_inputs = {
            "timeline_dialogue_reviewed": file_record(merged_path),
            "scene_dialogue_audit": file_record(audit_path),
        }
        summary_packet_config = {
            "contract": "phase1-video-summary-packet/v1",
            "model_content_authored": False,
        }
        summary_packet_signature = stage_signature(
            "summary_packet",
            summary_packet_inputs,
            summary_packet_config,
            implementation["digest"],
        )
        summary_packet_dir = asset_root / "summary" / summary_packet_signature[:16]
        summary_packet_path = summary_packet_dir / "video-summary-packet.json"
        summary_packet_command = [
            *cli_prefix(),
            "build-video-summary-packet",
            str(merged_path),
            "--output",
            str(summary_packet_path),
        ]
        statuses.append(
            execute_stage(
                asset_root=asset_root,
                state_path=state_path,
                state=state,
                name="summary_packet",
                signature=summary_packet_signature,
                inputs=summary_packet_inputs,
                config=summary_packet_config,
                commands=[summary_packet_command],
                runner=runner,
                after_commands=lambda _: verify_summary_packet(
                    prepared, merged_path, summary_packet_path
                ),
                collect_outputs=lambda: {
                    "video_summary_packet": file_record(summary_packet_path)
                },
            )
        )

        if not summary_review_path.is_file():
            state["current_outputs"] = {
                "timeline_dialogue_reviewed": str(merged_path),
                "scene_dialogue_audit": str(audit_path),
                "video_summary_packet": str(summary_packet_path),
            }
            _write_waiting_state(
                asset_root,
                state_path,
                state,
                status="awaiting_summary",
                expected_path=summary_review_path,
            )
            return {
                "asset_id": asset_id,
                "source_relative_path": source_relative_path,
                "story_day": story_day,
                "status": "awaiting_summary",
                "expected_summary": str(summary_review_path),
                "summary_packet": str(summary_packet_path),
                "timeline_dialogue_reviewed": str(merged_path),
                "state": str(state_path),
            }

        final_inputs = {
            "timeline_dialogue_reviewed": file_record(merged_path),
            "video_summary_packet": file_record(summary_packet_path),
            "model_video_summary": file_record(summary_review_path),
        }
        final_config = {
            "web_assets": "relative",
            "frame_limit": frame_limit,
        }
        final_signature = stage_signature(
            "summary_merge_web",
            final_inputs,
            final_config,
            implementation["digest"],
        )
        final_dir = asset_root / "final" / final_signature[:16]
        summarized_path = final_dir / "timeline.dialogue-reviewed.summarized.json"
        web_dir = final_dir / "web"
        final_commands = [
            [
                *cli_prefix(),
                "validate-video-summary",
                str(summary_packet_path),
                str(summary_review_path),
            ],
            [
                *cli_prefix(),
                "merge-video-summary",
                str(merged_path),
                str(summary_packet_path),
                str(summary_review_path),
                "--output",
                str(summarized_path),
            ],
            [
                *cli_prefix(),
                "render-web",
                str(summarized_path),
                "--output-dir",
                str(web_dir),
                "--assets",
                "relative",
                "--frame-limit",
                str(frame_limit),
            ],
        ]

        def after_final(_: list[dict[str, str]]) -> None:
            verify_summarized_timeline(prepared, summary_review_path, summarized_path)
            if (
                not (web_dir / "index.html").is_file()
                or not (web_dir / "manifest.json").is_file()
            ):
                raise ValueError(
                    "Relative-assets web renderer produced incomplete output"
                )

        statuses.append(
            execute_stage(
                asset_root=asset_root,
                state_path=state_path,
                state=state,
                name="summary_merge_web",
                signature=final_signature,
                inputs=final_inputs,
                config=final_config,
                commands=final_commands,
                runner=runner,
                after_commands=after_final,
                collect_outputs=lambda: {
                    "timeline_dialogue_reviewed_summarized": file_record(
                        summarized_path
                    ),
                    "web": directory_record(web_dir),
                },
            )
        )
        state["status"] = "completed"
        state["completed_at"] = utc_now()
        state.pop("expected_model_output", None)
        state["current_outputs"] = {
            "timeline_dialogue_reviewed": str(merged_path),
            "scene_dialogue_audit": str(audit_path),
            "video_summary_packet": str(summary_packet_path),
            "timeline_dialogue_reviewed_summarized": str(summarized_path),
            "web_index": str(web_dir / "index.html"),
        }
        update_state(state_path, state)
        return {
            "asset_id": asset_id,
            "source_relative_path": source_relative_path,
            "story_day": story_day,
            "status": "reused"
            if all(item == "reused" for item in statuses)
            else "completed",
            "state": str(state_path),
            "outputs": state["current_outputs"],
            "stage_statuses": {
                name: state["stages"][name]["status"]
                for name in (
                    "review_merge",
                    "summary_packet",
                    "summary_merge_web",
                )
            },
        }
    except StageLockedError as error:
        return {
            "asset_id": asset_id,
            "source_relative_path": source_relative_path,
            "story_day": story_day,
            "status": "locked",
            "error": {"type": type(error).__name__, "message": str(error)},
            "state": str(state_path),
        }
    except Exception as error:
        failed = {
            "schema_version": FINISHER_STATE_SCHEMA,
            "asset_id": asset_id,
            "source_relative_path": source_relative_path,
            "story_day": story_day,
            "status": "failed",
            "updated_at": utc_now(),
            "error": {"type": type(error).__name__, "message": error_text(error)},
        }
        try:
            with nonblocking_lock(asset_root / "locks/failure.lock"):
                if state_path.is_file():
                    current = load_json(state_path)
                    current.update(failed)
                    failed = current
                atomic_json(state_path, failed)
        except Exception:
            pass
        return {
            "asset_id": asset_id,
            "source_relative_path": source_relative_path,
            "story_day": story_day,
            "status": "failed",
            "error": failed["error"],
            "state": str(state_path),
        }


def finish_batch(
    preparation_manifest_path: Path,
    review_root: Path,
    output_root: Path,
    *,
    story_days: list[str] | None = None,
    asset_regex: str | None = None,
    jobs: int = 1,
    frame_limit: int = 16,
    runner: Runner = run_cli,
) -> dict[str, Any]:
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    if frame_limit < 4:
        raise ValueError("frame_limit must be at least 4")
    preparation_manifest_path = preparation_manifest_path.expanduser().resolve()
    review_root = review_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    preparation = load_json(preparation_manifest_path)
    source_manifest, source_manifest_record = preparation_source_manifest(
        preparation, preparation_manifest_path
    )
    output_root_is_safe(source_manifest, output_root)
    records = select_records(preparation, story_days or [], asset_regex)
    output_root.mkdir(parents=True, exist_ok=True)
    implementation = implementation_record()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {
            executor.submit(
                finish_asset,
                record,
                review_root,
                output_root,
                implementation,
                runner,
                frame_limit,
            ): record
            for record in records
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(
        key=lambda item: (str(item["story_day"]), str(item["source_relative_path"]))
    )
    statuses = (
        "completed",
        "reused",
        "awaiting_review",
        "awaiting_summary",
        "skipped_preparation",
        "locked",
        "failed",
    )
    summary = {
        "selected_asset_count": len(results),
        **{
            status: sum(item["status"] == status for item in results)
            for status in statuses
        },
    }
    day_manifests: dict[str, str] = {}
    selected_days = sorted({str(item["story_day"]) for item in results})
    for story_day in selected_days:
        day_results = [item for item in results if item["story_day"] == story_day]
        day_path = output_root / "days" / story_day / "manifest.json"
        atomic_json(
            day_path,
            {
                "schema_version": FINISHER_DAY_MANIFEST_SCHEMA,
                "generated_at": utc_now(),
                "story_day": story_day,
                "preparation_manifest": file_record(preparation_manifest_path),
                "review_root": str(review_root),
                "summary": {
                    "selected_asset_count": len(day_results),
                    **{
                        status: sum(item["status"] == status for item in day_results)
                        for status in statuses
                    },
                },
                "records": day_results,
            },
        )
        day_manifests[story_day] = str(day_path)
    errors = [item for item in results if item["status"] in {"failed", "locked"}]
    errors_path = output_root / "errors.json"
    atomic_json(
        errors_path,
        {
            "schema_version": "golden-v3-finisher-errors/v1",
            "generated_at": utc_now(),
            "count": len(errors),
            "records": errors,
        },
    )
    result = {
        "schema_version": FINISHER_MANIFEST_SCHEMA,
        "generated_at": utc_now(),
        "preparation_manifest": file_record(preparation_manifest_path),
        "story_day_source_manifest": source_manifest_record,
        "review_root": str(review_root),
        "review_path_convention": {
            "scene_dialogue_review": (
                "<review-root>/<asset_id>/scene-dialogue/review.json"
            ),
            "video_summary": "<review-root>/<asset_id>/summary/video-summary.json",
        },
        "output_root": str(output_root),
        "selection": {
            "story_days": story_days or selected_days,
            "asset_regex": asset_regex,
            "jobs": jobs,
            "frame_limit": frame_limit,
        },
        "implementation": implementation,
        "policy": {
            "model_content_authored": False,
            "dialogue_policy_required": DIALOGUE_POLICY,
            "caption_coverage_ratio_required": 1.0,
            "visual_moment_assignment_required": "exactly_once",
            "web_assets": "relative",
            "media_modified": False,
        },
        "summary": summary,
        "records": results,
        "day_manifests": day_manifests,
        "errors": str(errors_path),
    }
    atomic_json(output_root / "manifest.json", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and merge Golden v3 model reviews, build summary packets, "
            "and render relative-assets review pages when model summaries exist"
        )
    )
    parser.add_argument(
        "preparation_manifest",
        type=Path,
        help="Golden v3 preparation root manifest or one day manifest",
    )
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--story-day",
        action="append",
        default=[],
        help="Limit a root manifest to this story day; repeat as needed",
    )
    parser.add_argument(
        "--asset-regex", help="Optional regex matched against asset ID/source path"
    )
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--frame-limit", type=int, default=16)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = finish_batch(
            args.preparation_manifest,
            args.review_root,
            args.output_root,
            story_days=args.story_day,
            asset_regex=args.asset_regex,
            jobs=args.jobs,
            frame_limit=args.frame_limit,
        )
    except Exception as error:  # noqa: BLE001 - CLI boundary maps failures to status.
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    incomplete = manifest["summary"]["failed"] + manifest["summary"]["locked"]
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
