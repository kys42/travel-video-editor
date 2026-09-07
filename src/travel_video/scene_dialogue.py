from __future__ import annotations

import copy
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from .clip_evidence import (contract as clip_evidence_contract, validate_clip_evidence,
                            namespace_clip_evidence, validate_person_observations)
from .phase1 import atomic_json, format_time
from .review import load_json
from .transcript_reconcile import (
    RECONCILIATION_PACKET_SCHEMA,
    create_reconciliation_packet,
)

SCENE_DIALOGUE_PACKET_SCHEMA = "scene-dialogue-review-packet/v1"
SCENE_DIALOGUE_REVIEW_SCHEMA = "scene-dialogue-review/v1"
REVIEWED_DIALOGUE_SCHEMA = "reviewed-dialogue/v1"
REVIEWED_DIALOGUE_ATTACHMENT_SCHEMA = "timeline-reviewed-dialogue-attachment/v1"
DIALOGUE_PRESERVATION_POLICY = "dialogue-preservation/v1"
BOUNDARY_PROPOSAL_SCHEMA = "boundary-proposal/v1"
VISUAL_MOMENT_SCHEMA = "visual-moment/v1"
VISUAL_MOMENT_ROLES = {"visual", "action", "candid"}

SUPPORTED_TIMELINE_SCHEMAS = {
    "phase1-reviewed-timeline/v1",
    "phase1-context-reviewed-timeline/v1",
    "phase1-video-summarized-timeline/v1",
}
VALID_LANGUAGES = {"ko", "en", "mixed", "uncertain"}
CAPTION_LANGUAGES = {"ko", "en", "mixed"}
WINDOW_STATUSES = {"resolved", "uncertain", "non_speech"}
REVIEW_STATUSES = {"reviewed", "verified"}
EDIT_TYPES = {"verbatim", "normalized", "condensed", "paraphrase"}
EDITORIAL_BEAT_TYPES = {
    "visual",
    "action",
    "dialogue",
    "mixed",
    "transition",
    "ambience",
}
DIALOGUE_CLOSURES = {"closed", "open", "not_applicable"}
BOUNDARY_ADJUSTMENTS = {
    "none",
    "extend_before",
    "extend_after",
    "merge_previous",
    "merge_next",
}
EPSILON = 0.001
CAPTION_LEAD_SECONDS = 1.0
CAPTION_TAIL_SECONDS = 1.5


def _normalized_source_path(value: Any) -> str:
    path = os.path.normpath(os.path.expanduser(str(value)))
    return unicodedata.normalize("NFC", path)


def _validate_source_identity(
    timeline: dict[str, Any], reconciliation_packet: dict[str, Any]
) -> None:
    timeline_source = timeline.get("source", {})
    packet_source = reconciliation_packet.get("source", {})
    timeline_path = timeline_source.get("path")
    packet_path = packet_source.get("path")
    if not timeline_path or not packet_path:
        raise ValueError("Timeline and reconciliation packet must record source.path")
    if _normalized_source_path(timeline_path) != _normalized_source_path(packet_path):
        raise ValueError("Reconciliation packet source path does not match timeline")
    timeline_fingerprint = timeline_source.get("quick_fingerprint")
    packet_fingerprint = packet_source.get("quick_fingerprint")
    if not timeline_fingerprint or not packet_fingerprint:
        raise ValueError(
            "Timeline and reconciliation packet must record source.quick_fingerprint"
        )
    if str(timeline_fingerprint) != str(packet_fingerprint):
        raise ValueError("Reconciliation packet fingerprint does not match timeline")


def _timeline_groups(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    context_groups = timeline.get("context_groups")
    if isinstance(context_groups, list) and context_groups:
        return context_groups
    reviewed_groups = timeline.get("reviewed_groups")
    if not isinstance(reviewed_groups, list) or not reviewed_groups:
        raise ValueError("Timeline must contain reviewed_groups or context_groups")
    segments = {
        str(segment.get("segment_id", "")): segment
        for segment in timeline.get("segments", [])
    }
    groups: list[dict[str, Any]] = []
    for group in reviewed_groups:
        segment_ids = [str(item) for item in group.get("segment_ids", [])]
        if not segment_ids or any(item not in segments for item in segment_ids):
            raise ValueError("Reviewed groups must cite valid timeline segments")
        selected = [segments[item] for item in segment_ids]
        start = float(selected[0]["start"])
        end = float(selected[-1]["end"])
        groups.append(
            {
                **copy.deepcopy(group),
                "start": start,
                "end": end,
                "timecode": f"{format_time(start)}-{format_time(end)}",
            }
        )
    return groups


def _validate_timeline(timeline: dict[str, Any]) -> float:
    if timeline.get("schema_version") not in SUPPORTED_TIMELINE_SCHEMAS:
        raise ValueError(
            "Scene dialogue review requires a reviewed, context-reviewed, or summarized timeline"
        )
    duration = float(timeline.get("media", {}).get("duration", 0.0))
    if duration <= 0:
        raise ValueError("Timeline media duration must be positive")
    groups = _timeline_groups(timeline)
    seen: set[str] = set()
    previous_start = -1.0
    previous_end = -1.0
    for group in groups:
        group_id = str(group.get("group_id", "")).strip()
        if not group_id or group_id in seen:
            raise ValueError("Timeline context group IDs must be present and unique")
        seen.add(group_id)
        start = float(group["start"])
        end = float(group["end"])
        if start < 0 or end <= start or end > duration + EPSILON:
            raise ValueError(f"Context group {group_id} is outside timeline duration")
        if start < previous_start or start < previous_end - EPSILON:
            raise ValueError(
                "Timeline context groups must be ordered and non-overlapping"
            )
        previous_start = start
        previous_end = end
    return duration


def _overlap(start: float, end: float, other_start: float, other_end: float) -> float:
    return max(0.0, min(end, other_end) - max(start, other_start))


def _validate_reconciliation_windows(
    packet: dict[str, Any], duration: float
) -> list[dict[str, Any]]:
    if packet.get("schema_version") != RECONCILIATION_PACKET_SCHEMA:
        raise ValueError("Expected transcript-reconciliation-packet/v1")
    windows = packet.get("windows")
    if not isinstance(windows, list):
        raise ValueError("Reconciliation packet windows must be a list")
    seen: set[str] = set()
    previous_start = -1.0
    for window in windows:
        window_id = str(window.get("window_id", "")).strip()
        if not window_id or window_id in seen:
            raise ValueError("Reconciliation window IDs must be present and unique")
        seen.add(window_id)
        start = float(window["start"])
        end = float(window["end"])
        if start < 0 or end <= start or end > duration + EPSILON:
            raise ValueError(f"Reconciliation window {window_id} is outside media")
        if start < previous_start:
            raise ValueError("Reconciliation windows must be in chronological order")
        previous_start = start
        candidates = window.get("apple_candidates")
        if not isinstance(candidates, dict) or not candidates:
            raise ValueError(f"Reconciliation window {window_id} has no candidates")
        for locale, candidate in candidates.items():
            source_ids = candidate.get("source_candidate_ids")
            if not isinstance(source_ids, list) or not source_ids:
                raise ValueError(f"Candidate {window_id}/{locale} has no source IDs")
            if len(source_ids) != len(set(source_ids)):
                raise ValueError(
                    f"Candidate {window_id}/{locale} has duplicate source IDs"
                )
            spans = candidate.get("evidence_spans")
            if not isinstance(spans, list) or not spans:
                raise ValueError(
                    f"Candidate {window_id}/{locale} has no evidence spans"
                )
            for span in spans:
                if span.get("source_candidate_id") not in source_ids:
                    raise ValueError(
                        f"Evidence span in {window_id}/{locale} has an invalid source ID"
                    )
                span_start = float(span["start"])
                span_end = float(span["end"])
                if span_start < 0 or span_end <= span_start:
                    raise ValueError(f"Invalid evidence span in {window_id}/{locale}")
                if _overlap(start, end, span_start, span_end) <= 0:
                    raise ValueError(
                        f"Evidence span in {window_id}/{locale} does not overlap its window"
                    )
    return windows


def _validate_boundary_proposal_item(
    proposal: dict[str, Any],
    *,
    duration: float,
    cluster_tolerance: float,
) -> tuple[str, float]:
    proposal_id = str(proposal.get("proposal_id", "")).strip()
    if not proposal_id:
        raise ValueError("Boundary proposals require proposal_id")
    timestamp = float(proposal.get("timestamp", -1.0))
    if timestamp < 0 or timestamp > duration + EPSILON:
        raise ValueError(f"Boundary proposal {proposal_id} is outside media")
    timecode = str(proposal.get("timecode", "")).strip()
    expected_timecode = format_time(timestamp)
    accepted_timecodes = {expected_timecode}
    if expected_timecode.count(":") == 1:
        accepted_timecodes.add(f"00:{expected_timecode}")
    if timecode not in accepted_timecodes:
        raise ValueError(
            f"Boundary proposal {proposal_id} timecode does not match timestamp"
        )
    _validate_confidence(proposal, f"Boundary proposal {proposal_id}")
    primary_kind = str(proposal.get("primary_kind", "")).strip()
    if not primary_kind:
        raise ValueError(f"Boundary proposal {proposal_id} requires primary_kind")
    evidence = proposal.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(f"Boundary proposal {proposal_id} requires evidence")
    kinds: set[str] = set()
    source_ids: set[str] = set()
    for item in evidence:
        kind = str(item.get("kind", "")).strip()
        source_id = str(item.get("source_id", "")).strip()
        if not kind or not source_id:
            raise ValueError(
                f"Boundary proposal {proposal_id} evidence requires kind and source_id"
            )
        if source_id in source_ids:
            raise ValueError(
                f"Boundary proposal {proposal_id} has duplicate evidence source IDs"
            )
        source_ids.add(source_id)
        kinds.add(kind)
        evidence_timestamp = float(item.get("timestamp", -1.0))
        if evidence_timestamp < 0 or evidence_timestamp > duration + EPSILON:
            raise ValueError(
                f"Boundary proposal {proposal_id} evidence is outside media"
            )
        if abs(evidence_timestamp - timestamp) > cluster_tolerance + EPSILON:
            raise ValueError(
                f"Boundary proposal {proposal_id} evidence exceeds cluster tolerance"
            )
        _validate_confidence(
            item,
            f"Boundary proposal {proposal_id} evidence {source_id}",
        )
        if not isinstance(item.get("details", {}), dict):
            raise ValueError(
                f"Boundary proposal {proposal_id} evidence details must be an object"
            )
    if primary_kind not in kinds:
        raise ValueError(
            f"Boundary proposal {proposal_id} primary_kind is absent from evidence"
        )
    return proposal_id, timestamp


def _load_boundary_proposals(
    path: Path,
    *,
    timeline: dict[str, Any],
    duration: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = load_json(path)
    if payload.get("schema_version") != BOUNDARY_PROPOSAL_SCHEMA:
        raise ValueError("Expected boundary-proposal/v1")
    if payload.get("asset_id") != timeline.get("asset_id"):
        raise ValueError("Boundary proposal asset_id does not match timeline")
    _validate_source_identity(timeline, {"source": payload.get("source", {})})
    proposal_duration = float(payload.get("media", {}).get("duration", 0.0))
    if abs(proposal_duration - duration) > EPSILON:
        raise ValueError("Boundary proposal duration does not match timeline")
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("Boundary proposal policy must be an object")
    cluster_tolerance = float(policy.get("cluster_tolerance_seconds", -1.0))
    if cluster_tolerance < 0:
        raise ValueError("Boundary proposal cluster tolerance must be non-negative")
    neighbor_context = float(policy.get("neighbor_context_seconds", 5.0))
    if neighbor_context < 0:
        raise ValueError("Boundary proposal neighbor context must be non-negative")
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("Boundary proposals must be a list")
    seen_ids: set[str] = set()
    previous_timestamp = -1.0
    normalized: list[dict[str, Any]] = []
    for proposal in proposals:
        proposal_id, timestamp = _validate_boundary_proposal_item(
            proposal,
            duration=duration,
            cluster_tolerance=cluster_tolerance,
        )
        if proposal_id in seen_ids:
            raise ValueError("Boundary proposal IDs must be unique")
        if timestamp <= previous_timestamp + EPSILON:
            raise ValueError(
                "Boundary proposals must be chronological with unique timestamps"
            )
        seen_ids.add(proposal_id)
        previous_timestamp = timestamp
        normalized.append(copy.deepcopy(proposal))
    summary = payload.get("summary")
    if summary is not None:
        if not isinstance(summary, dict):
            raise ValueError("Boundary proposal summary must be an object")
        if int(summary.get("proposal_count", -1)) != len(normalized):
            raise ValueError("Boundary proposal summary count is inconsistent")
    return normalized, copy.deepcopy(policy)


def _scene_boundary_proposals(
    proposals: list[dict[str, Any]],
    *,
    start: float,
    end: float,
    neighbor_context: float,
) -> dict[str, Any]:
    candidates = [
        copy.deepcopy(proposal)
        for proposal in proposals
        if start - EPSILON <= float(proposal["timestamp"]) <= end + EPSILON
    ]
    previous = next(
        (
            copy.deepcopy(proposal)
            for proposal in reversed(proposals)
            if start - neighbor_context - EPSILON
            <= float(proposal["timestamp"])
            < start - EPSILON
        ),
        None,
    )
    following = next(
        (
            copy.deepcopy(proposal)
            for proposal in proposals
            if end + EPSILON
            < float(proposal["timestamp"])
            <= end + neighbor_context + EPSILON
        ),
        None,
    )
    return {
        "candidate_proposals": candidates,
        "previous_proposal": previous,
        "next_proposal": following,
    }


def validate_boundary_proposals(
    boundary_proposals_path: Path,
    timeline_path: Path,
) -> None:
    boundary_proposals_path = boundary_proposals_path.expanduser().resolve()
    timeline_path = timeline_path.expanduser().resolve()
    timeline = load_json(timeline_path)
    duration = _validate_timeline(timeline)
    _load_boundary_proposals(
        boundary_proposals_path,
        timeline=timeline,
        duration=duration,
    )


def _load_visual_moments(
    path: Path,
    *,
    timeline: dict[str, Any],
    duration: float,
) -> list[dict[str, Any]]:
    payload = load_json(path)
    if payload.get("schema_version") != VISUAL_MOMENT_SCHEMA:
        raise ValueError(f"Expected {VISUAL_MOMENT_SCHEMA}")
    if payload.get("asset_id") != timeline.get("asset_id"):
        raise ValueError("Visual moment asset_id does not match timeline")
    _validate_source_identity(timeline, {"source": payload.get("source", {})})
    moment_duration = float(payload.get("media", {}).get("duration", 0.0))
    if abs(moment_duration - duration) > EPSILON:
        raise ValueError("Visual moment duration does not match timeline")
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("Visual moment policy must be an object")
    if policy.get("speech_is_selection_filter") is not False:
        raise ValueError("Visual moment discovery must not filter candidates by speech")
    moments = payload.get("moments")
    if not isinstance(moments, list):
        raise ValueError("Visual moments must be a list")

    seen_ids: set[str] = set()
    seen_sample_ids: set[str] = set()
    previous_timestamp = -1.0
    normalized: list[dict[str, Any]] = []
    for moment in moments:
        moment_id = str(moment.get("moment_id", "")).strip()
        sample_id = str(moment.get("representative_sample_id", "")).strip()
        if not moment_id or moment_id in seen_ids:
            raise ValueError("Visual moment IDs must be present and unique")
        if not sample_id or sample_id in seen_sample_ids:
            raise ValueError("Visual moment sample IDs must be present and unique")
        seen_ids.add(moment_id)
        seen_sample_ids.add(sample_id)
        start = float(moment.get("start", -1.0))
        end = float(moment.get("end", -1.0))
        timestamp = float(moment.get("representative_timestamp", -1.0))
        if start < 0 or end <= start or end > duration + EPSILON:
            raise ValueError(f"Visual moment {moment_id} is outside media")
        if timestamp < start - EPSILON or timestamp > end + EPSILON:
            raise ValueError(
                f"Visual moment {moment_id} representative timestamp is outside its range"
            )
        if timestamp <= previous_timestamp + EPSILON:
            raise ValueError("Visual moments must have chronological unique timestamps")
        previous_timestamp = timestamp
        if str(moment.get("timecode", "")) != (
            f"{format_time(start)}-{format_time(end)}"
        ):
            raise ValueError(f"Visual moment {moment_id} timecode does not match range")
        if str(moment.get("representative_timecode", "")) != format_time(timestamp):
            raise ValueError(
                f"Visual moment {moment_id} representative timecode does not match"
            )
        frame = Path(str(moment.get("representative_frame", ""))).expanduser()
        if not frame.is_absolute() or not frame.is_file():
            raise ValueError(
                f"Visual moment {moment_id} representative frame is missing"
            )
        primary_role = str(moment.get("primary_role", ""))
        roles = moment.get("roles")
        if primary_role not in VISUAL_MOMENT_ROLES:
            raise ValueError(f"Visual moment {moment_id} has an invalid primary role")
        if (
            not isinstance(roles, list)
            or not roles
            or len(roles) != len(set(roles))
            or set(roles) - VISUAL_MOMENT_ROLES
            or primary_role not in roles
        ):
            raise ValueError(f"Visual moment {moment_id} has invalid roles")
        _validate_confidence(moment, f"Visual moment {moment_id}")
        score = float(moment.get("score", -1.0))
        if score < 0 or score > 1:
            raise ValueError(f"Visual moment {moment_id} score must be in [0, 1]")
        if not isinstance(moment.get("speech_free"), bool):
            raise ValueError(f"Visual moment {moment_id} requires speech_free")
        speech_overlap = float(moment.get("speech_overlap_seconds", -1.0))
        if speech_overlap < 0 or speech_overlap > end - start + EPSILON:
            raise ValueError(f"Visual moment {moment_id} has invalid speech overlap")
        evidence = moment.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"Visual moment {moment_id} requires evidence")
        evidence_ids: set[str] = set()
        for item in evidence:
            source_id = str(item.get("source_id", "")).strip()
            kind = str(item.get("kind", "")).strip()
            evidence_time = float(item.get("timestamp", -1.0))
            if not source_id or not kind or source_id in evidence_ids:
                raise ValueError(f"Visual moment {moment_id} has invalid evidence IDs")
            if evidence_time < 0 or evidence_time > duration + EPSILON:
                raise ValueError(f"Visual moment {moment_id} evidence is outside media")
            if not isinstance(item.get("details"), dict):
                raise ValueError(f"Visual moment {moment_id} evidence requires details")
            evidence_ids.add(source_id)
        normalized.append(copy.deepcopy(moment))

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("Visual moment summary must be an object")
    if int(summary.get("moment_count", -1)) != len(normalized):
        raise ValueError("Visual moment summary count is inconsistent")
    if int(summary.get("speech_free_moment_count", -1)) != sum(
        1 for moment in normalized if moment["speech_free"]
    ):
        raise ValueError("Visual moment speech-free summary count is inconsistent")
    return normalized


def validate_visual_moments(
    visual_moments_path: Path,
    timeline_path: Path,
) -> None:
    visual_moments_path = visual_moments_path.expanduser().resolve()
    timeline_path = timeline_path.expanduser().resolve()
    timeline = load_json(timeline_path)
    duration = _validate_timeline(timeline)
    _load_visual_moments(
        visual_moments_path,
        timeline=timeline,
        duration=duration,
    )


def _evidence_id(window_id: str, locale: str, index: int) -> str:
    safe_locale = re.sub(r"[^A-Za-z0-9]+", "-", locale).strip("-")
    return f"{window_id}-{safe_locale}-E{index:03d}"


def _enriched_window(window: dict[str, Any]) -> dict[str, Any]:
    enriched = copy.deepcopy(window)
    # Existing context dialogue summaries may have been authored from an older
    # reconciliation pass. Keep visual evidence, but do not leak that answer into
    # an independent one-pass review.
    enriched.pop("scene_context", None)
    window_start = float(window["start"])
    window_end = float(window["end"])
    for locale, candidate in enriched["apple_candidates"].items():
        for index, span in enumerate(candidate["evidence_spans"], start=1):
            span["evidence_id"] = _evidence_id(
                str(window["window_id"]), str(locale), index
            )
            span["usable_start"] = round(max(window_start, float(span["start"])), 3)
            span["usable_end"] = round(min(window_end, float(span["end"])), 3)
    return enriched


def _visual_context(timeline: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
    group_start = float(group["start"])
    group_end = float(group["end"])
    segment_ids = set(group.get("segment_ids", []))
    segments: list[dict[str, Any]] = []
    for segment in timeline.get("segments", []):
        if segment_ids and segment.get("segment_id") not in segment_ids:
            continue
        if (
            not segment_ids
            and _overlap(
                group_start,
                group_end,
                float(segment["start"]),
                float(segment["end"]),
            )
            <= 0
        ):
            continue
        review = segment.get("review", {})
        segments.append(
            {
                "segment_id": segment.get("segment_id"),
                "start": segment.get("start"),
                "end": segment.get("end"),
                "visual_summary": review.get("visual_summary"),
                "actions": copy.deepcopy(review.get("actions", [])),
                "importance": review.get("importance"),
                "machine_flags": copy.deepcopy(
                    segment.get("machine", {}).get("flags", [])
                ),
            }
        )

    context_review = group.get("context_review", {})
    referenced_samples: list[str] = []
    representative = group.get("representative_sample_id") or context_review.get(
        "representative_sample_id"
    )
    if representative:
        referenced_samples.append(str(representative))
    for moment in context_review.get("key_moments", []):
        if moment.get("sample_id"):
            referenced_samples.append(str(moment["sample_id"]))
    for moment in context_review.get("notable_moments", []):
        if moment.get("sample_id"):
            referenced_samples.append(str(moment["sample_id"]))
    wanted = set(referenced_samples)
    samples = [
        {
            key: copy.deepcopy(sample.get(key))
            for key in ("sample_id", "time", "timecode", "frame")
        }
        for sample in timeline.get("samples", [])
        if sample.get("sample_id") in wanted
    ]
    return {
        "narrative_summary": context_review.get("narrative_summary"),
        "representative_sample_id": representative,
        "representative_reason": context_review.get("representative_reason"),
        "key_moments": copy.deepcopy(context_review.get("key_moments", [])),
        "notable_moments": copy.deepcopy(context_review.get("notable_moments", [])),
        "segments": segments,
        "samples": samples,
    }


def _integrated_visual_contexts(
    visual_packet_path: Path,
    timeline: dict[str, Any],
    groups: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    visual_packet = load_json(visual_packet_path)
    if visual_packet.get("schema_version") != "phase1-context-packet/v1":
        raise ValueError("Integrated visual input must be phase1-context-packet/v1")
    if visual_packet.get("asset_id") != timeline.get("asset_id"):
        raise ValueError("Integrated visual packet asset_id does not match timeline")
    packet_groups = visual_packet.get("groups")
    if not isinstance(packet_groups, list):
        raise ValueError("Integrated visual packet groups must be a list")
    expected_ids = [str(group["group_id"]) for group in groups]
    actual_ids = [str(group.get("group_id", "")) for group in packet_groups]
    if actual_ids != expected_ids:
        raise ValueError(
            "Integrated visual packet groups must match timeline group order"
        )

    timeline_samples = {
        str(sample.get("sample_id")): sample
        for sample in timeline.get("samples", [])
        if sample.get("sample_id")
    }
    timeline_segments = {
        str(segment.get("segment_id")): segment
        for segment in timeline.get("segments", [])
    }
    contexts: dict[str, dict[str, Any]] = {}
    for timeline_group, packet_group in zip(groups, packet_groups, strict=True):
        group_id = str(timeline_group["group_id"])
        for field in ("start", "end"):
            if abs(float(packet_group[field]) - float(timeline_group[field])) > EPSILON:
                raise ValueError(f"Integrated visual packet changed {group_id} {field}")
        if list(packet_group.get("segment_ids", [])) != list(
            timeline_group.get("segment_ids", [])
        ):
            raise ValueError(
                f"Integrated visual packet changed {group_id} segment coverage"
            )
        candidate_frames = copy.deepcopy(packet_group.get("candidate_frames", []))
        if not isinstance(candidate_frames, list) or not candidate_frames:
            raise ValueError(
                f"Integrated visual packet group {group_id} has no candidate frames"
            )
        candidate_ids = [
            str(candidate.get("sample_id", "")) for candidate in candidate_frames
        ]
        if (
            any(not item for item in candidate_ids)
            or len(candidate_ids) != len(set(candidate_ids))
            or set(candidate_ids) - set(timeline_samples)
        ):
            raise ValueError(
                f"Integrated visual packet group {group_id} has invalid sample IDs"
            )
        for candidate in candidate_frames:
            sample_id = str(candidate["sample_id"])
            source_sample = timeline_samples[sample_id]
            if (
                abs(float(candidate.get("time", -1)) - float(source_sample["time"]))
                > EPSILON
            ):
                raise ValueError(
                    f"Integrated visual packet changed sample {sample_id} time"
                )
            if str(candidate.get("timecode", "")) != str(
                source_sample.get("timecode", "")
            ):
                raise ValueError(
                    f"Integrated visual packet changed sample {sample_id} timecode"
                )
            if _normalized_source_path(candidate.get("frame", "")) != (
                _normalized_source_path(source_sample.get("frame", ""))
            ):
                raise ValueError(
                    f"Integrated visual packet changed sample {sample_id} frame"
                )
        storyboard = visual_packet_path.parent / str(packet_group.get("storyboard", ""))
        if not storyboard.is_file():
            raise ValueError(
                f"Integrated visual packet storyboard is missing for {group_id}"
            )
        segment_summaries = []
        for item in packet_group.get("segment_summaries", []):
            segment_id = str(item.get("segment_id", ""))
            source_segment = timeline_segments.get(segment_id)
            if source_segment is None:
                raise ValueError(
                    f"Integrated visual packet group {group_id} cites invalid segment"
                )
            segment_summaries.append(
                {
                    **copy.deepcopy(item),
                    "start": source_segment.get("start"),
                    "end": source_segment.get("end"),
                }
            )
        contexts[group_id] = {
            "review_stage": "quick_group_plus_dense_storyboard",
            "label": packet_group.get("label"),
            "prior_summary": packet_group.get("prior_summary"),
            "segment_summaries": segment_summaries,
            "storyboard": str(storyboard.resolve()),
            "candidate_frames": candidate_frames,
        }
    return contexts


def _review_contract(
    *,
    integrated_visual_review: bool = False,
    boundary_proposals_available: bool = False,
    visual_moments_available: bool = False,
    clip_evidence_required: bool = False,
) -> dict[str, Any]:
    scene_required = [
        "group_id",
        "window_decisions",
        "utterances",
        "captions",
    ]
    if integrated_visual_review:
        scene_required.extend(["scene_understanding", "editorial_beats"])
    contract = {
        "schema_version": SCENE_DIALOGUE_REVIEW_SCHEMA,
        "top_level_required": ["schema_version", "reviewer", "scenes"],
        "scene_order": "exactly match packet.scenes; include scenes with no speech",
        "scene_required": scene_required,
        "window_decision": {
            "required": ["window_id", "status", "source_utterance_ids"],
            "status": sorted(WINDOW_STATUSES),
            "coverage": "exactly one decision per packet window, in packet order",
            "uncertain_requires": "notes",
        },
        "utterance": {
            "required": [
                "utterance_id",
                "start",
                "end",
                "language",
                "original_text",
                "translations",
                "confidence",
                "review_status",
                "source_evidence_ids",
                "source_window_ids",
                "source_candidate_ids",
            ],
            "language": sorted(VALID_LANGUAGES),
            "review_status": sorted(REVIEW_STATUSES),
            "source_rule": "all IDs must resolve to cited packet evidence",
            "uncertain_requires": "notes and no caption-usable lexical wording",
            "preservation_rule": (
                "low ASR confidence alone is not a reason to discard speech; assign "
                "ko/en/mixed whenever the spoken language and a useful wording can be "
                "recovered, and reserve uncertain for genuinely unusable fragments"
            ),
        },
        "caption": {
            "required": [
                "caption_id",
                "start",
                "end",
                "display_text",
                "language",
                "confidence",
                "review_status",
                "edit_type",
                "source_utterance_ids",
                "source_window_ids",
                "source_candidate_ids",
            ],
            "language": sorted(CAPTION_LANGUAGES),
            "review_status": sorted(REVIEW_STATUSES),
            "edit_type": sorted(EDIT_TYPES),
            "coverage": (
                "every plausible lexical ko/en/mixed utterance must feed at least one "
                "caption, including useful turns inside an uncertain window"
            ),
            "partial_text": (
                "keep the reliable words and use punctuation or an ellipsis instead of "
                "dropping the whole turn; use normalized for context-supported cleanup"
            ),
            "uncertain_sources": (
                "forbidden; first recover any useful fragment as ko/en/mixed, and leave "
                "only meaningless or unidentifiable residue uncertain"
            ),
        },
        "compact_example": {
            "schema_version": SCENE_DIALOGUE_REVIEW_SCHEMA,
            "reviewer": "model-name",
            "method": "one-pass scene dialogue review",
            "scenes": [
                {
                    "group_id": "G001",
                    "window_decisions": [
                        {
                            "window_id": "RW0001",
                            "status": "resolved",
                            "source_utterance_ids": ["G001-U001"],
                        }
                    ],
                    "utterances": [
                        {
                            "utterance_id": "G001-U001",
                            "start": 1.2,
                            "end": 2.8,
                            "language": "ko",
                            "original_text": "실제로 들린 원문",
                            "translations": {},
                            "confidence": 0.9,
                            "review_status": "reviewed",
                            "source_evidence_ids": ["RW0001-ko-KR-E001"],
                            "source_window_ids": ["RW0001"],
                            "source_candidate_ids": ["APPLE-ko-KR-T0001"],
                        }
                    ],
                    "captions": [
                        {
                            "caption_id": "G001-C001",
                            "start": 1.2,
                            "end": 3.0,
                            "display_text": "자막용으로 정리한 문장",
                            "language": "ko",
                            "confidence": 0.9,
                            "review_status": "reviewed",
                            "edit_type": "normalized",
                            "source_utterance_ids": ["G001-U001"],
                            "source_window_ids": ["RW0001"],
                            "source_candidate_ids": ["APPLE-ko-KR-T0001"],
                        }
                    ],
                }
            ],
        },
    }
    if integrated_visual_review:
        contract["scene_understanding"] = {
            "required": [
                "label",
                "narrative_summary",
                "actions",
                "representative_sample_id",
                "notable_moments",
                "confidence",
                "boundary_notes",
            ],
            "evidence_rule": "sample IDs must come from scene visual_context",
            "boundary_rule": (
                "note any speech window crossing a coarse group boundary"
            ),
        }
        contract["editorial_beat"] = {
            "required": [
                "beat_id",
                "beat_type",
                "start",
                "end",
                "title",
                "summary",
                "source_segment_ids",
                "source_window_ids",
                "source_utterance_ids",
                "source_caption_ids",
                "representative_sample_ids",
                "dialogue_closure",
                "boundary_adjustment",
                "boundary_reason",
                "confidence",
            ],
            "conditional_required": {
                "source_boundary_proposal_ids": (
                    "when the scene exposes candidate/neighbor boundary proposals"
                ),
                "source_visual_moment_ids": (
                    "when scene visual_context.visual_moments is non-empty"
                ),
            },
            "beat_type": sorted(EDITORIAL_BEAT_TYPES),
            "dialogue_closure": sorted(DIALOGUE_CLOSURES),
            "boundary_adjustment": sorted(BOUNDARY_ADJUSTMENTS),
            "coverage": (
                "every scene window, utterance, and caption belongs to exactly one beat"
            ),
            "timing": (
                "ordered non-overlapping evidence-anchored ranges; a beat may cross the "
                "coarse group boundary only when cited evidence crosses it"
            ),
            "boundary_proposal_rule": (
                "when boundary proposals are available, cite the proposal IDs used for "
                "start or end; an uncited proposal is only a suggestion"
            ),
            "visual_moment_rule": (
                "inspect speech-free visual/action/candid moments independently of "
                "dialogue and cite every selected moment ID"
            ),
        }
        example_scene = contract["compact_example"]["scenes"][0]
        example_scene["scene_understanding"] = {
            "label": "표를 확인하고 플랫폼으로 이동",
            "narrative_summary": "표를 확인한 뒤 플랫폼 방향으로 걷는다.",
            "actions": ["표 확인", "이동"],
            "representative_sample_id": "F0001",
            "notable_moments": [],
            "confidence": 0.9,
            "boundary_notes": [],
        }
        example_scene["editorial_beats"] = [
            {
                "beat_id": "G001-B001",
                "beat_type": "mixed",
                "start": 1.0,
                "end": 3.2,
                "title": "기차를 타러 이동",
                "summary": "표를 확인하며 기차를 타러 가야 한다고 말한다.",
                "source_segment_ids": ["S001"],
                "source_window_ids": ["RW0001"],
                "source_utterance_ids": ["G001-U001"],
                "source_caption_ids": ["G001-C001"],
                "representative_sample_ids": ["F0001"],
                **(
                    {"source_visual_moment_ids": ["VM0001"]}
                    if visual_moments_available
                    else {}
                ),
                "dialogue_closure": "closed",
                "boundary_adjustment": "none",
                "boundary_reason": "한 문장과 표 확인 행동이 함께 끝난다.",
                "confidence": 0.9,
                **(
                    {"source_boundary_proposal_ids": ["BP0001"]}
                    if boundary_proposals_available
                    else {}
                ),
            }
        ]
    if clip_evidence_required:
        contract["editorial_beat"]["required"].append("clip_evidence")
        contract["clip_evidence"] = clip_evidence_contract()
    return contract


def build_scene_dialogue_packet(
    apple_transcript_path: Path,
    timeline_path: Path,
    output_path: Path,
    *,
    mlx_normalized_path: Path | None = None,
    max_window: float = 8.0,
    visual_packet_path: Path | None = None,
    boundary_proposals_path: Path | None = None,
    visual_moments_path: Path | None = None,
    clip_evidence: bool = False,
) -> Path:
    if clip_evidence and visual_packet_path is None:
        raise ValueError("Clip evidence requires integrated --visual-packet review")
    apple_transcript_path = apple_transcript_path.expanduser().resolve()
    timeline_path = timeline_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if visual_packet_path is not None:
        visual_packet_path = visual_packet_path.expanduser().resolve()
    if boundary_proposals_path is not None:
        boundary_proposals_path = boundary_proposals_path.expanduser().resolve()
        if visual_packet_path is None:
            raise ValueError(
                "Boundary proposals require --visual-packet integrated review"
            )
    if visual_moments_path is not None:
        visual_moments_path = visual_moments_path.expanduser().resolve()
        if visual_packet_path is None:
            raise ValueError("Visual moments require --visual-packet integrated review")
    if output_path in {
        apple_transcript_path,
        timeline_path,
        *([visual_packet_path] if visual_packet_path is not None else []),
        *(
            [boundary_proposals_path]
            if boundary_proposals_path is not None
            else []
        ),
        *([visual_moments_path] if visual_moments_path is not None else []),
    }:
        raise ValueError("Scene dialogue packet output must be a new file")
    timeline = load_json(timeline_path)
    if (
        timeline.get("schema_version") == "phase1-reviewed-timeline/v1"
        and visual_packet_path is None
    ):
        raise ValueError(
            "A phase1 reviewed timeline requires --visual-packet for integrated review"
        )
    reconciliation_packet = create_reconciliation_packet(
        apple_transcript_path,
        mlx_normalized_path=mlx_normalized_path,
        timeline_path=None,
        max_window=max_window,
    )
    duration = _validate_timeline(timeline)
    _validate_source_identity(timeline, reconciliation_packet)
    windows = _validate_reconciliation_windows(reconciliation_packet, duration)
    groups = _timeline_groups(timeline)
    boundary_proposals: list[dict[str, Any]] | None = None
    boundary_policy: dict[str, Any] | None = None
    if boundary_proposals_path is not None:
        boundary_proposals, boundary_policy = _load_boundary_proposals(
            boundary_proposals_path,
            timeline=timeline,
            duration=duration,
        )
    boundary_proposals_available = bool(boundary_proposals)
    visual_moments: list[dict[str, Any]] | None = None
    if visual_moments_path is not None:
        visual_moments = _load_visual_moments(
            visual_moments_path,
            timeline=timeline,
            duration=duration,
        )
    visual_moments_available = bool(visual_moments)
    integrated_contexts = (
        _integrated_visual_contexts(visual_packet_path, timeline, groups)
        if visual_packet_path is not None
        else None
    )

    assignments: dict[str, list[dict[str, Any]]] = {
        str(group["group_id"]): [] for group in groups
    }
    for window in windows:
        start = float(window["start"])
        end = float(window["end"])
        overlaps = [
            (
                _overlap(start, end, float(group["start"]), float(group["end"])),
                index,
                group,
            )
            for index, group in enumerate(groups)
        ]
        best_overlap, _, best_group = max(
            overlaps, key=lambda item: (item[0], -item[1])
        )
        if best_overlap <= 0:
            raise ValueError(
                f"Reconciliation window {window['window_id']} is not covered by a context group"
            )
        assignments[str(best_group["group_id"])].append(_enriched_window(window))

    scenes = []
    for group_index, group in enumerate(groups):
        group_id = str(group["group_id"])
        group_windows = assignments[group_id]
        visual_context = (
            copy.deepcopy(integrated_contexts[group_id])
            if integrated_contexts is not None
            else _visual_context(timeline, group)
        )
        group_moments = [
            copy.deepcopy(moment)
            for moment in visual_moments or []
            if (
                float(group["start"]) - EPSILON
                <= float(moment["representative_timestamp"])
                < float(group["end"]) - EPSILON
            )
            or (
                group_index == len(groups) - 1
                and abs(
                    float(moment["representative_timestamp"])
                    - float(group["end"])
                )
                <= EPSILON
            )
        ]
        if visual_moments is not None:
            visual_context["visual_moments"] = group_moments
            visual_context["candidate_frames"].extend(
                {
                    "sample_id": moment["representative_sample_id"],
                    "time": moment["representative_timestamp"],
                    "timecode": moment["representative_timecode"],
                    "frame": moment["representative_frame"],
                    "evidence_origin": VISUAL_MOMENT_SCHEMA,
                    "visual_moment_id": moment["moment_id"],
                    "primary_role": moment["primary_role"],
                    "score": moment["score"],
                    "speech_free": moment["speech_free"],
                }
                for moment in group_moments
            )
        scenes.append(
            {
                "group_id": group_id,
                "label": group.get("label"),
                "start": group["start"],
                "end": group["end"],
                "timecode": group.get("timecode")
                or f"{format_time(float(group['start']))}-{format_time(float(group['end']))}",
                "segment_ids": copy.deepcopy(group.get("segment_ids", [])),
                "visual_context": visual_context,
                "window_ids": [item["window_id"] for item in group_windows],
                "windows": group_windows,
            }
        )

    owner_by_window = {
        str(window["window_id"]): str(scene["group_id"])
        for scene in scenes
        for window in scene["windows"]
    }
    for scene in scenes:
        scene_start = float(scene["start"])
        scene_end = float(scene["end"])
        crossing = [
            {
                "window_id": window["window_id"],
                "start": window["start"],
                "end": window["end"],
                "assigned_group_id": owner_by_window[str(window["window_id"])],
            }
            for window in windows
            if (
                float(window["start"]) < scene_start < float(window["end"])
                or float(window["start"]) < scene_end < float(window["end"])
            )
        ]
        scene["boundary_context"] = {
            "crossing_windows": crossing,
            "previous_window": next(
                (
                    {
                        "window_id": window["window_id"],
                        "start": window["start"],
                        "end": window["end"],
                        "assigned_group_id": owner_by_window[str(window["window_id"])],
                    }
                    for window in reversed(windows)
                    if float(window["end"]) <= scene_start + EPSILON
                ),
                None,
            ),
            "next_window": next(
                (
                    {
                        "window_id": window["window_id"],
                        "start": window["start"],
                        "end": window["end"],
                        "assigned_group_id": owner_by_window[str(window["window_id"])],
                    }
                    for window in windows
                    if float(window["start"]) >= scene_end - EPSILON
                ),
                None,
            ),
        }
        if boundary_proposals_available:
            scene["boundary_context"].update(
                _scene_boundary_proposals(
                    boundary_proposals,
                    start=scene_start,
                    end=scene_end,
                    neighbor_context=float(
                        (boundary_policy or {}).get("neighbor_context_seconds", 5.0)
                    ),
                )
            )

    if boundary_proposals_path is not None and clip_evidence:
        observations = load_json(boundary_proposals_path).get("person_observations")
        if observations is not None:
            validate_person_observations(observations, duration)
            for scene in scenes:
                # Only sampled detections near supplied frames are sent to the model.
                # Full positive detections remain in the boundary artifact.
                times = [float(f["time"]) for f in scene["visual_context"]["candidate_frames"]]
                cadence = float(observations["sample_interval_seconds"])
                scene["person_observations"] = {**copy.deepcopy(observations),
                    "observations": [copy.deepcopy(o) for o in observations["observations"]
                        if any(abs(float(o["timestamp"]) - t) <= cadence + EPSILON for t in times)]}

    packet = {
        "schema_version": SCENE_DIALOGUE_PACKET_SCHEMA,
        "asset_id": timeline.get("asset_id"),
        "source": copy.deepcopy(timeline.get("source")),
        "media": {"duration": duration},
        "inputs": {
            "apple_transcript": str(apple_transcript_path),
            "apple_raw": copy.deepcopy(
                reconciliation_packet.get("inputs", {}).get("apple_raw", {})
            ),
            "mlx_normalized": reconciliation_packet.get("inputs", {}).get(
                "mlx_normalized"
            ),
            "timeline": str(timeline_path),
            "visual_packet": (
                str(visual_packet_path) if visual_packet_path is not None else None
            ),
            "boundary_proposals": (
                str(boundary_proposals_path)
                if boundary_proposals_path is not None
                else None
            ),
            "visual_moments": (
                str(visual_moments_path) if visual_moments_path is not None else None
            ),
        },
        "fresh_reconciliation": {
            "schema_version": reconciliation_packet["schema_version"],
            "detector": copy.deepcopy(reconciliation_packet.get("detector")),
            "policy": copy.deepcopy(reconciliation_packet.get("policy", {})),
            "summary": copy.deepcopy(reconciliation_packet.get("summary", {})),
        },
        "policy": {
            "policy_version": DIALOGUE_PRESERVATION_POLICY,
            "canonical_for_editing": True,
            "review_unit": "timeline_context_group",
            "model_passes": 1,
            "window_assignment": "greatest_temporal_overlap_then_earliest_group",
            "preserve_original_language": True,
            "visual_context_is_non_authoritative": True,
            "all_windows_require_explicit_decisions": True,
            "all_resolved_utterances_require_caption_coverage": True,
            "preservation_mode": "recall_first_plausible_speech",
            "uncertain_window_does_not_block_captions": True,
            "drop_only": "non_speech_or_no_caption_usable_lexical_content",
            "integrated_visual_review": visual_packet_path is not None,
            "editorial_beats_required": visual_packet_path is not None,
            "clip_evidence_required": clip_evidence,
            "boundary_proposals_supplied": boundary_proposals_path is not None,
            "boundary_proposals_available": boundary_proposals_available,
            "boundary_proposals_are_advisory": True,
            "visual_moments_supplied": visual_moments_path is not None,
            "visual_moments_available": visual_moments_available,
            "speech_free_visual_discovery_required": visual_moments_path is not None,
        },
        "instructions": [
            "Review every scene once using timed Apple evidence and adjacent visual context.",
            "Return evidence-grounded utterances and caption-ready display lines separately.",
            "Select the actually spoken language; split Korean-English turns when needed.",
            "Never use visual context to invent words that are absent from audio evidence.",
            "Preserve original_text; put cleanup only in caption display_text and edit_type.",
            "Preserve plausible speech even when wording is partial or low-confidence.",
            "An uncertain window may still contain caption-ready ko/en/mixed turns.",
            "Use language=uncertain only for residue with no caption-usable wording; do not hide an otherwise useful turn.",
            "For a clipped sentence, keep reliable words with an ellipsis or apply context-supported normalization and record the uncertainty.",
            "Drop only non-speech or meaningless fragments, never a turn merely because one word is uncertain.",
        ],
        "review_contract": _review_contract(
            integrated_visual_review=visual_packet_path is not None,
            clip_evidence_required=clip_evidence,
            boundary_proposals_available=boundary_proposals_available,
            visual_moments_available=visual_moments_available,
        ),
        "summary": {
            "scene_count": len(scenes),
            "window_count": len(windows),
            "evidence_span_count": sum(
                len(candidate.get("evidence_spans", []))
                for window in windows
                for candidate in window.get("apple_candidates", {}).values()
            ),
            "boundary_proposal_count": len(boundary_proposals or []),
            "visual_moment_count": len(visual_moments or []),
            "speech_free_visual_moment_count": sum(
                1 for moment in visual_moments or [] if moment["speech_free"]
            ),
        },
        **(
            {
                "boundary_evidence": {
                    "schema_version": BOUNDARY_PROPOSAL_SCHEMA,
                    "policy": boundary_policy,
                    "proposal_count": len(boundary_proposals or []),
                }
            }
            if boundary_proposals is not None
            else {}
        ),
        "scenes": scenes,
    }
    if visual_packet_path is not None:
        packet["instructions"].extend(
            [
                "Author scene_understanding from the dense storyboard and timed speech together.",
                "Split each coarse group into evidence-anchored editorial_beats for visual, action, and dialogue flow.",
                "Assign every window, utterance, and caption to exactly one editorial beat.",
                "Do not cut a complete utterance; flag coarse boundaries crossed by speech.",
            ]
        )
    if boundary_proposals_available:
        packet["instructions"].extend(
            [
                "Treat boundary proposals as advisory local evidence, never as mandatory cuts.",
                "Use visual/action/dialogue closure to accept, reject, merge, or ignore proposals.",
                "Cite every boundary proposal used to anchor an editorial beat boundary.",
            ]
        )
    if visual_moments_available:
        packet["instructions"].extend(
            [
                "Inspect every visual_moment even when no speech window overlaps it.",
                "Treat visual, action, and candid moments as independent highlight evidence.",
                "Cite source_visual_moment_ids for moments used in editorial beats.",
            ]
        )
    if clip_evidence:
        packet["instructions"].append("Also return clip_evidence per beat using its exact versioned contract. Separate visual observations, speech mentions and inference; empty lists remain unknown.")
    validate_scene_dialogue_packet(packet, timeline)
    atomic_json(output_path, packet)
    return output_path


def validate_scene_dialogue_packet(
    packet: dict[str, Any], timeline: dict[str, Any] | None = None
) -> None:
    if packet.get("schema_version") != SCENE_DIALOGUE_PACKET_SCHEMA:
        raise ValueError("Unsupported scene dialogue packet schema")
    scenes = packet.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("Scene dialogue packet must contain scenes")
    duration = float(packet.get("media", {}).get("duration", 0.0))
    if duration <= 0:
        raise ValueError("Scene dialogue packet duration must be positive")
    for scene in packet.get("scenes", []):
        if "person_observations" in scene:
            validate_person_observations(scene["person_observations"], float(packet["media"]["duration"]))
    contract = packet.get("review_contract", {})
    if contract.get("schema_version") != SCENE_DIALOGUE_REVIEW_SCHEMA:
        raise ValueError("Scene dialogue packet is missing its review contract")
    fresh_summary = packet.get("fresh_reconciliation", {}).get("summary", {})
    if (
        fresh_summary.get("missing_candidate_ids")
        or int(fresh_summary.get("missing_evidence_span_count", -1)) != 0
    ):
        raise ValueError("Scene dialogue packet does not preserve fresh Apple evidence")
    scene_ids = [str(scene.get("group_id", "")) for scene in scenes]
    if any(not item for item in scene_ids) or len(scene_ids) != len(set(scene_ids)):
        raise ValueError("Scene dialogue packet group IDs must be present and unique")
    window_ids: list[str] = []
    evidence_ids: list[str] = []
    boundary_evidence = packet.get("boundary_evidence")
    boundary_proposals_available = packet.get("policy", {}).get(
        "boundary_proposals_available", False
    )
    visual_moments_supplied = packet.get("policy", {}).get(
        "visual_moments_supplied", False
    )
    visual_moments_available = packet.get("policy", {}).get(
        "visual_moments_available", False
    )
    if boundary_evidence is not None:
        if not isinstance(boundary_evidence, dict):
            raise ValueError("Boundary packet evidence must be an object")
        if boundary_evidence.get("schema_version") != BOUNDARY_PROPOSAL_SCHEMA:
            raise ValueError("Boundary-aware packet has an invalid schema")
        proposal_policy = boundary_evidence.get("policy")
        if not isinstance(proposal_policy, dict):
            raise ValueError("Boundary-aware packet requires proposal policy")
        cluster_tolerance = float(
            proposal_policy.get("cluster_tolerance_seconds", -1.0)
        )
        if cluster_tolerance < 0:
            raise ValueError("Boundary proposal cluster tolerance must be non-negative")
        neighbor_context = float(proposal_policy.get("neighbor_context_seconds", 5.0))
        if neighbor_context < 0:
            raise ValueError("Boundary proposal neighbor context must be non-negative")
    else:
        if boundary_proposals_available:
            raise ValueError("Boundary-aware packet requires boundary_evidence")
        cluster_tolerance = 0.0
        neighbor_context = 0.0
    all_boundary_proposals: dict[str, dict[str, Any]] = {}
    candidate_boundary_ids: set[str] = set()
    visual_moment_ids: set[str] = set()
    visual_moment_sample_ids: set[str] = set()
    for scene in scenes:
        declared = scene.get("window_ids")
        windows = scene.get("windows")
        if not isinstance(declared, list) or not isinstance(windows, list):
            raise ValueError(f"Invalid windows in scene {scene['group_id']}")
        actual = [str(window.get("window_id", "")) for window in windows]
        if declared != actual:
            raise ValueError(f"Window list mismatch in scene {scene['group_id']}")
        window_ids.extend(actual)
        for window in windows:
            for locale, candidate in window.get("apple_candidates", {}).items():
                spans = candidate.get("evidence_spans", [])
                if not spans:
                    raise ValueError(
                        f"Missing evidence spans in {window['window_id']}/{locale}"
                    )
                for span in spans:
                    evidence_id = str(span.get("evidence_id", ""))
                    if not evidence_id:
                        raise ValueError("Packet evidence spans require evidence_id")
                    usable_start = float(span["usable_start"])
                    usable_end = float(span["usable_end"])
                    if usable_start < 0 or usable_end <= usable_start:
                        raise ValueError(f"Invalid usable evidence {evidence_id}")
                    evidence_ids.append(evidence_id)
        visual_context = scene.get("visual_context", {})
        scene_moments = visual_context.get("visual_moments")
        if visual_moments_supplied:
            if not isinstance(scene_moments, list):
                raise ValueError(
                    f"Scene {scene['group_id']} requires visual_moments evidence"
                )
            candidate_frames = {
                str(frame.get("sample_id", "")): frame
                for frame in visual_context.get("candidate_frames", [])
            }
            for moment in scene_moments:
                moment_id = str(moment.get("moment_id", ""))
                sample_id = str(moment.get("representative_sample_id", ""))
                timestamp = float(moment.get("representative_timestamp", -1.0))
                if not moment_id or moment_id in visual_moment_ids:
                    raise ValueError(
                        "Visual moment IDs must occur in exactly one packet scene"
                    )
                if not sample_id or sample_id in visual_moment_sample_ids:
                    raise ValueError(
                        "Visual moment sample IDs must occur in exactly one packet scene"
                    )
                if timestamp < float(scene["start"]) - EPSILON or timestamp > float(
                    scene["end"]
                ) + EPSILON:
                    raise ValueError(
                        f"Scene {scene['group_id']} contains an out-of-range visual moment"
                    )
                frame = candidate_frames.get(sample_id)
                if frame is None:
                    raise ValueError(
                        f"Visual moment {moment_id} has no candidate frame in its scene"
                    )
                if (
                    abs(float(frame.get("time", -1.0)) - timestamp) > EPSILON
                    or str(frame.get("frame", ""))
                    != str(moment.get("representative_frame", ""))
                    or str(frame.get("visual_moment_id", "")) != moment_id
                ):
                    raise ValueError(
                        f"Visual moment {moment_id} candidate frame changed in packet"
                    )
                visual_moment_ids.add(moment_id)
                visual_moment_sample_ids.add(sample_id)
        if boundary_proposals_available:
            context = scene.get("boundary_context", {})
            candidates = context.get("candidate_proposals")
            if not isinstance(candidates, list):
                raise ValueError(
                    f"Scene {scene['group_id']} requires candidate boundary proposals"
                )
            previous_boundary_time = -1.0
            for proposal in candidates:
                proposal_id, timestamp = _validate_boundary_proposal_item(
                    proposal,
                    duration=duration,
                    cluster_tolerance=cluster_tolerance,
                )
                if timestamp < float(scene["start"]) - EPSILON or timestamp > float(
                    scene["end"]
                ) + EPSILON:
                    raise ValueError(
                        f"Scene {scene['group_id']} contains an out-of-range boundary proposal"
                    )
                if timestamp <= previous_boundary_time + EPSILON:
                    raise ValueError(
                        f"Scene {scene['group_id']} boundary proposals must be chronological"
                    )
                previous_boundary_time = timestamp
                if proposal_id in candidate_boundary_ids:
                    existing = all_boundary_proposals.get(proposal_id)
                    if existing != proposal:
                        raise ValueError(
                            f"Boundary proposal {proposal_id} changed between scenes"
                        )
                candidate_boundary_ids.add(proposal_id)
                all_boundary_proposals.setdefault(proposal_id, proposal)
            for relation, comparator in (
                ("previous_proposal", "previous"),
                ("next_proposal", "next"),
            ):
                proposal = context.get(relation)
                if proposal is None:
                    continue
                proposal_id, timestamp = _validate_boundary_proposal_item(
                    proposal,
                    duration=duration,
                    cluster_tolerance=cluster_tolerance,
                )
                if comparator == "previous" and timestamp >= float(
                    scene["start"]
                ) - EPSILON:
                    raise ValueError(
                        f"Scene {scene['group_id']} previous proposal is not previous"
                    )
                if comparator == "next" and timestamp <= float(
                    scene["end"]
                ) + EPSILON:
                    raise ValueError(
                        f"Scene {scene['group_id']} next proposal is not next"
                    )
                boundary_distance = (
                    float(scene["start"]) - timestamp
                    if comparator == "previous"
                    else timestamp - float(scene["end"])
                )
                if boundary_distance > neighbor_context + EPSILON:
                    raise ValueError(
                        f"Scene {scene['group_id']} {comparator} proposal is too distant"
                    )
                existing = all_boundary_proposals.get(proposal_id)
                if existing is not None and existing != proposal:
                    raise ValueError(
                        f"Boundary proposal {proposal_id} changed between scenes"
                    )
                all_boundary_proposals.setdefault(proposal_id, proposal)
    if len(window_ids) != len(set(window_ids)):
        raise ValueError("Each reconciliation window must occur in exactly one scene")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("Scene dialogue evidence IDs must be globally unique")
    if int(packet.get("summary", {}).get("window_count", -1)) != len(window_ids):
        raise ValueError("Scene dialogue packet window summary is inconsistent")
    if int(packet.get("summary", {}).get("scene_count", -1)) != len(scenes):
        raise ValueError("Scene dialogue packet scene summary is inconsistent")
    if int(packet.get("summary", {}).get("evidence_span_count", -1)) != len(
        evidence_ids
    ):
        raise ValueError("Scene dialogue packet evidence summary is inconsistent")
    if boundary_evidence is not None:
        expected_proposals = int(boundary_evidence.get("proposal_count", -1))
        if bool(expected_proposals) != bool(boundary_proposals_available):
            raise ValueError("Boundary proposal availability policy is inconsistent")
        if expected_proposals != len(all_boundary_proposals):
            raise ValueError("Boundary packet proposal coverage is inconsistent")
        if int(packet.get("summary", {}).get("boundary_proposal_count", -1)) != (
            expected_proposals
        ):
            raise ValueError("Boundary proposal summary is inconsistent")
    if bool(visual_moment_ids) != bool(visual_moments_available):
        raise ValueError("Visual moment availability policy is inconsistent")
    if int(packet.get("summary", {}).get("visual_moment_count", 0)) != len(
        visual_moment_ids
    ):
        raise ValueError("Visual moment summary is inconsistent")
    speech_free_moment_count = sum(
        1
        for scene in scenes
        for moment in scene.get("visual_context", {}).get("visual_moments", [])
        if moment.get("speech_free") is True
    )
    if int(
        packet.get("summary", {}).get("speech_free_visual_moment_count", 0)
    ) != speech_free_moment_count:
        raise ValueError("Speech-free visual moment summary is inconsistent")
    if timeline is not None:
        _validate_timeline(timeline)
        packet_source = {"source": packet.get("source", {})}
        _validate_source_identity(timeline, packet_source)
        timeline_groups = _timeline_groups(timeline)
        expected = [str(group["group_id"]) for group in timeline_groups]
        if scene_ids != expected:
            raise ValueError("Packet scenes must match timeline context group order")
        for scene, group in zip(scenes, timeline_groups, strict=True):
            if (
                abs(float(scene["start"]) - float(group["start"])) > EPSILON
                or abs(float(scene["end"]) - float(group["end"])) > EPSILON
            ):
                raise ValueError(f"Packet scene bounds changed for {scene['group_id']}")


def _scene_evidence(scene: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for window in scene["windows"]:
        for candidate in window["apple_candidates"].values():
            for span in candidate["evidence_spans"]:
                evidence[str(span["evidence_id"])] = {
                    **span,
                    "window_id": window["window_id"],
                    "source_candidate_id": span["source_candidate_id"],
                }
    return evidence


def _require_unique_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    items = [str(item) for item in value]
    if any(not item for item in items) or len(items) != len(set(items)):
        raise ValueError(f"{field} must contain unique non-empty IDs")
    return items


def _validate_confidence(item: dict[str, Any], label: str) -> None:
    value = item.get("confidence")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label} confidence must be numeric")
    if not 0 <= float(value) <= 1:
        raise ValueError(f"{label} confidence must be between 0 and 1")


def _optional_unique_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    items = [str(item) for item in value]
    if any(not item for item in items) or len(items) != len(set(items)):
        raise ValueError(f"{field} must contain unique non-empty IDs")
    return items


def _near_anchor(value: float, anchors: set[float]) -> bool:
    return any(abs(value - anchor) <= EPSILON for anchor in anchors)


def _scene_proposal_map(scene: dict[str, Any]) -> dict[str, dict[str, Any]]:
    context = scene.get("boundary_context", {})
    proposals = list(context.get("candidate_proposals", []))
    for key in ("previous_proposal", "next_proposal"):
        if context.get(key) is not None:
            proposals.append(context[key])
    return {str(item["proposal_id"]): item for item in proposals}


def _validate_integrated_scene_review(
    packet_scene: dict[str, Any],
    reviewed_scene: dict[str, Any],
    utterance_map: dict[str, dict[str, Any]],
    captions: list[dict[str, Any]],
    duration: float,
    boundary_proposals_available: bool,
    visual_moments_available: bool,
) -> None:
    group_id = str(packet_scene["group_id"])
    understanding = reviewed_scene.get("scene_understanding")
    if not isinstance(understanding, dict):
        raise ValueError(f"Integrated scene {group_id} requires scene_understanding")
    for field in ("label", "narrative_summary"):
        if not str(understanding.get(field, "")).strip():
            raise ValueError(f"Scene {group_id} understanding requires {field}")
    actions = understanding.get("actions")
    if not isinstance(actions, list) or not all(
        str(action).strip() for action in actions
    ):
        raise ValueError(f"Scene {group_id} understanding actions must be a list")
    notable_moments = understanding.get("notable_moments")
    if not isinstance(notable_moments, list):
        raise ValueError(
            f"Scene {group_id} understanding notable_moments must be a list"
        )
    boundary_notes = understanding.get("boundary_notes")
    if not isinstance(boundary_notes, list) or not all(
        str(note).strip() for note in boundary_notes
    ):
        raise ValueError(
            f"Scene {group_id} understanding boundary_notes must be a list"
        )
    _validate_confidence(understanding, f"Scene understanding {group_id}")

    visual_context = packet_scene.get("visual_context", {})
    candidate_frames = visual_context.get("candidate_frames", [])
    allowed_sample_ids = {str(item.get("sample_id", "")) for item in candidate_frames}
    representative = str(understanding.get("representative_sample_id", ""))
    if not representative or representative not in allowed_sample_ids:
        raise ValueError(
            f"Scene {group_id} representative sample is not in visual evidence"
        )
    for moment in notable_moments:
        sample_id = str(moment.get("sample_id", ""))
        if sample_id and sample_id not in allowed_sample_ids:
            raise ValueError(
                f"Scene {group_id} notable moment cites invalid sample {sample_id}"
            )
        for field in ("sample_id", "category", "title", "description", "edit_hint"):
            if not str(moment.get(field, "")).strip():
                raise ValueError(f"Scene {group_id} notable moment requires {field}")

    beats = reviewed_scene.get("editorial_beats")
    if not isinstance(beats, list) or not beats:
        raise ValueError(f"Integrated scene {group_id} requires editorial_beats")
    allowed_window_ids = [str(item) for item in packet_scene["window_ids"]]
    allowed_segment_ids = [str(item) for item in packet_scene.get("segment_ids", [])]
    caption_map = {str(item["caption_id"]): item for item in captions}
    legacy_anchors = {
        float(packet_scene["start"]),
        float(packet_scene["end"]),
        0.0,
        duration,
    }
    for item in visual_context.get("segment_summaries", []):
        legacy_anchors.update((float(item["start"]), float(item["end"])))
    for item in candidate_frames:
        legacy_anchors.add(float(item["time"]))
    for window in packet_scene["windows"]:
        legacy_anchors.update((float(window["start"]), float(window["end"])))
    for item in utterance_map.values():
        legacy_anchors.update((float(item["start"]), float(item["end"])))
    for item in captions:
        legacy_anchors.update((float(item["start"]), float(item["end"])))
    proposal_map = _scene_proposal_map(packet_scene)
    scene_boundary_proposals_available = bool(proposal_map)
    proposal_anchors = {
        float(proposal["timestamp"]) for proposal in proposal_map.values()
    }
    anchors = legacy_anchors | proposal_anchors
    visual_moment_map = {
        str(moment["moment_id"]): moment
        for moment in visual_context.get("visual_moments", [])
    }
    scene_visual_moments_available = bool(visual_moment_map)

    beat_ids: set[str] = set()
    seen_window_ids: list[str] = []
    seen_utterance_ids: list[str] = []
    seen_caption_ids: list[str] = []
    seen_visual_moment_ids: list[str] = []
    previous_end = -1.0
    for beat in beats:
        beat_id = str(beat.get("beat_id", "")).strip()
        if not beat_id or beat_id in beat_ids:
            raise ValueError(f"Scene {group_id} beat IDs must be present and unique")
        beat_ids.add(beat_id)
        if beat.get("beat_type") not in EDITORIAL_BEAT_TYPES:
            raise ValueError(f"Beat {beat_id} has an invalid beat_type")
        if beat.get("dialogue_closure") not in DIALOGUE_CLOSURES:
            raise ValueError(f"Beat {beat_id} has an invalid dialogue_closure")
        boundary_adjustment = beat.get("boundary_adjustment")
        if boundary_adjustment not in BOUNDARY_ADJUSTMENTS:
            raise ValueError(f"Beat {beat_id} has an invalid boundary_adjustment")
        if (
            not str(beat.get("title", "")).strip()
            or not str(beat.get("summary", "")).strip()
        ):
            raise ValueError(f"Beat {beat_id} requires title and summary")
        if not str(beat.get("boundary_reason", "")).strip():
            raise ValueError(f"Beat {beat_id} requires boundary_reason")
        _validate_confidence(beat, f"Beat {beat_id}")

        start = float(beat["start"])
        end = float(beat["end"])
        if start < 0 or end <= start or end > duration + EPSILON:
            raise ValueError(f"Beat {beat_id} is outside media duration")
        if start < previous_end - EPSILON:
            raise ValueError(f"Scene {group_id} editorial beats must not overlap")
        previous_end = end
        if not _near_anchor(start, anchors) or not _near_anchor(end, anchors):
            raise ValueError(f"Beat {beat_id} boundaries are not evidence-anchored")
        scene_start = float(packet_scene["start"])
        scene_end = float(packet_scene["end"])
        if start < scene_start - EPSILON and boundary_adjustment not in {
            "extend_before",
            "merge_previous",
        }:
            raise ValueError(
                f"Beat {beat_id} crosses the group start without adjustment"
            )
        if end > scene_end + EPSILON and boundary_adjustment not in {
            "extend_after",
            "merge_next",
        }:
            raise ValueError(f"Beat {beat_id} crosses the group end without adjustment")

        segment_ids = _require_unique_strings(
            beat.get("source_segment_ids"), f"Beat {beat_id} source_segment_ids"
        )
        if set(segment_ids) - set(allowed_segment_ids):
            raise ValueError(f"Beat {beat_id} cites invalid segment IDs")
        window_ids = _optional_unique_strings(
            beat.get("source_window_ids"), f"Beat {beat_id} source_window_ids"
        )
        utterance_ids = _optional_unique_strings(
            beat.get("source_utterance_ids"),
            f"Beat {beat_id} source_utterance_ids",
        )
        caption_ids = _optional_unique_strings(
            beat.get("source_caption_ids"), f"Beat {beat_id} source_caption_ids"
        )
        sample_ids = _optional_unique_strings(
            beat.get("representative_sample_ids"),
            f"Beat {beat_id} representative_sample_ids",
        )
        if set(window_ids) - set(allowed_window_ids):
            raise ValueError(f"Beat {beat_id} cites invalid window IDs")
        if set(utterance_ids) - set(utterance_map):
            raise ValueError(f"Beat {beat_id} cites invalid utterance IDs")
        if set(caption_ids) - set(caption_map):
            raise ValueError(f"Beat {beat_id} cites invalid caption IDs")
        if not sample_ids or set(sample_ids) - allowed_sample_ids:
            raise ValueError(f"Beat {beat_id} requires valid representative samples")
        if not window_ids and not sample_ids:
            raise ValueError(f"Beat {beat_id} has no visual or dialogue evidence")
        if visual_moments_available and scene_visual_moments_available:
            visual_moment_ids = _optional_unique_strings(
                beat.get("source_visual_moment_ids"),
                f"Beat {beat_id} source_visual_moment_ids",
            )
            if set(visual_moment_ids) - set(visual_moment_map):
                raise ValueError(f"Beat {beat_id} cites invalid visual moments")
            for visual_moment_id in visual_moment_ids:
                visual_moment = visual_moment_map[visual_moment_id]
                if _overlap(
                    start,
                    end,
                    float(visual_moment["start"]),
                    float(visual_moment["end"]),
                ) <= 0:
                    raise ValueError(
                        f"Beat {beat_id} does not overlap visual moment {visual_moment_id}"
                    )
                if visual_moment["representative_sample_id"] not in sample_ids:
                    raise ValueError(
                        f"Beat {beat_id} omits the representative sample for visual "
                        f"moment {visual_moment_id}"
                    )
            seen_visual_moment_ids.extend(visual_moment_ids)
        if boundary_proposals_available and scene_boundary_proposals_available:
            proposal_ids = _optional_unique_strings(
                beat.get("source_boundary_proposal_ids"),
                f"Beat {beat_id} source_boundary_proposal_ids",
            )
            if set(proposal_ids) - set(proposal_map):
                raise ValueError(f"Beat {beat_id} cites invalid boundary proposals")
            for proposal_id in proposal_ids:
                proposal_time = float(proposal_map[proposal_id]["timestamp"])
                if not _near_anchor(proposal_time, {start, end}):
                    raise ValueError(
                        f"Beat {beat_id} cites a proposal that does not anchor its boundary"
                    )
            for boundary in (start, end):
                if _near_anchor(boundary, legacy_anchors):
                    continue
                matching_ids = {
                    proposal_id
                    for proposal_id, proposal in proposal_map.items()
                    if abs(float(proposal["timestamp"]) - boundary) <= EPSILON
                }
                if matching_ids and not matching_ids.intersection(proposal_ids):
                    raise ValueError(
                        f"Beat {beat_id} must cite the proposal anchoring its boundary"
                    )
        for utterance_id in utterance_ids:
            utterance = utterance_map[utterance_id]
            if not set(utterance["source_window_ids"]).issubset(window_ids):
                raise ValueError(
                    f"Beat {beat_id} omits a source window for {utterance_id}"
                )
            if (
                float(utterance["start"]) < start - EPSILON
                or float(utterance["end"]) > end + EPSILON
            ):
                raise ValueError(f"Beat {beat_id} cuts utterance {utterance_id}")
        for caption_id in caption_ids:
            caption = caption_map[caption_id]
            if not set(caption["source_utterance_ids"]).issubset(utterance_ids):
                raise ValueError(
                    f"Beat {beat_id} omits a source utterance for {caption_id}"
                )
            if (
                float(caption["start"]) < start - EPSILON
                or float(caption["end"]) > end + EPSILON
            ):
                raise ValueError(f"Beat {beat_id} cuts caption {caption_id}")
        if caption_ids and beat.get("dialogue_closure") == "not_applicable":
            raise ValueError(f"Beat {beat_id} with captions needs dialogue closure")
        if beat.get("dialogue_closure") == "open" and boundary_adjustment == "none":
            raise ValueError(f"Open dialogue beat {beat_id} needs boundary adjustment")
        seen_window_ids.extend(window_ids)
        seen_utterance_ids.extend(utterance_ids)
        seen_caption_ids.extend(caption_ids)

    for label, seen, expected in (
        ("windows", seen_window_ids, allowed_window_ids),
        ("utterances", seen_utterance_ids, list(utterance_map)),
        ("captions", seen_caption_ids, list(caption_map)),
        *(
            [("visual moments", seen_visual_moment_ids, list(visual_moment_map))]
            if visual_moments_available and scene_visual_moments_available
            else []
        ),
    ):
        if len(seen) != len(set(seen)) or set(seen) != set(expected):
            raise ValueError(
                f"Scene {group_id} editorial beats must exactly cover {label}"
            )


def validate_scene_dialogue_review(
    packet: dict[str, Any], review: dict[str, Any]
) -> None:
    validate_scene_dialogue_packet(packet)
    if review.get("schema_version") != SCENE_DIALOGUE_REVIEW_SCHEMA:
        raise ValueError("Unsupported scene dialogue review schema")
    if not str(review.get("reviewer", "")).strip():
        raise ValueError("Scene dialogue review must identify its reviewer")
    expected_ids = [scene["group_id"] for scene in packet["scenes"]]
    reviewed_scenes = review.get("scenes")
    if not isinstance(reviewed_scenes, list):
        raise ValueError("Scene dialogue review scenes must be a list")
    reviewed_ids = [scene.get("group_id") for scene in reviewed_scenes]
    if reviewed_ids != expected_ids:
        raise ValueError("Review scenes must exactly match packet order and coverage")

    duration = float(packet["media"]["duration"])
    all_utterance_ids: set[str] = set()
    all_caption_ids: set[str] = set()
    all_beat_ids: set[str] = set()
    for packet_scene, reviewed_scene in zip(
        packet["scenes"], reviewed_scenes, strict=True
    ):
        group_id = str(packet_scene["group_id"])
        allowed_window_ids = [str(item) for item in packet_scene["window_ids"]]
        evidence_map = _scene_evidence(packet_scene)
        utterances = reviewed_scene.get("utterances")
        captions = reviewed_scene.get("captions")
        decisions = reviewed_scene.get("window_decisions")
        if not isinstance(utterances, list) or not isinstance(captions, list):
            raise ValueError(f"Scene {group_id} utterances and captions must be lists")
        if not isinstance(decisions, list):
            raise ValueError(f"Scene {group_id} window_decisions must be a list")
        decision_ids = [decision.get("window_id") for decision in decisions]
        if decision_ids != allowed_window_ids:
            raise ValueError(
                f"Scene {group_id} decisions must exactly cover its window order"
            )

        utterance_map: dict[str, dict[str, Any]] = {}
        previous_start = -1.0
        for utterance in utterances:
            utterance_id = str(utterance.get("utterance_id", "")).strip()
            if not utterance_id or utterance_id in all_utterance_ids:
                raise ValueError(
                    "Review utterance IDs must be present and globally unique"
                )
            all_utterance_ids.add(utterance_id)
            utterance_map[utterance_id] = utterance
            language = utterance.get("language")
            if language not in VALID_LANGUAGES:
                raise ValueError(
                    f"Invalid utterance language in {group_id}/{utterance_id}"
                )
            original_text = str(utterance.get("original_text", "")).strip()
            if language != "uncertain" and not original_text:
                raise ValueError(f"Missing original_text in {group_id}/{utterance_id}")
            if language == "uncertain" and not str(utterance.get("notes", "")).strip():
                raise ValueError(f"Uncertain utterance {utterance_id} requires notes")
            if not isinstance(utterance.get("translations", {}), dict):
                raise ValueError(
                    f"Utterance {utterance_id} translations must be an object"
                )
            if utterance.get("review_status") not in REVIEW_STATUSES:
                raise ValueError(f"Invalid review_status for utterance {utterance_id}")
            _validate_confidence(utterance, f"Utterance {utterance_id}")
            start = float(utterance["start"])
            end = float(utterance["end"])
            if start < 0 or end <= start or end > duration + EPSILON:
                raise ValueError(f"Utterance {utterance_id} is outside media duration")
            if start < previous_start:
                raise ValueError(
                    f"Utterances in scene {group_id} must be chronological"
                )
            previous_start = start

            source_evidence_ids = _require_unique_strings(
                utterance.get("source_evidence_ids"),
                f"Utterance {utterance_id} source_evidence_ids",
            )
            invalid_evidence = set(source_evidence_ids) - set(evidence_map)
            if invalid_evidence:
                raise ValueError(
                    f"Utterance {utterance_id} cites invalid evidence IDs: "
                    + ", ".join(sorted(invalid_evidence))
                )
            evidence_items = [evidence_map[item] for item in source_evidence_ids]
            expected_window_ids = list(
                dict.fromkeys(str(item["window_id"]) for item in evidence_items)
            )
            expected_candidate_ids = list(
                dict.fromkeys(
                    str(item["source_candidate_id"]) for item in evidence_items
                )
            )
            source_window_ids = _require_unique_strings(
                utterance.get("source_window_ids"),
                f"Utterance {utterance_id} source_window_ids",
            )
            source_candidate_ids = _require_unique_strings(
                utterance.get("source_candidate_ids"),
                f"Utterance {utterance_id} source_candidate_ids",
            )
            if set(source_window_ids) != set(expected_window_ids):
                raise ValueError(
                    f"Utterance {utterance_id} window IDs do not match evidence"
                )
            if set(source_candidate_ids) != set(expected_candidate_ids):
                raise ValueError(
                    f"Utterance {utterance_id} candidate IDs do not match evidence"
                )
            if any(
                _overlap(
                    start,
                    end,
                    float(item["usable_start"]),
                    float(item["usable_end"]),
                )
                <= 0
                for item in evidence_items
            ):
                raise ValueError(
                    f"Utterance {utterance_id} does not overlap all cited evidence"
                )
            evidence_start = min(float(item["usable_start"]) for item in evidence_items)
            evidence_end = max(float(item["usable_end"]) for item in evidence_items)
            if start < evidence_start - EPSILON or end > evidence_end + EPSILON:
                raise ValueError(
                    f"Utterance {utterance_id} timing exceeds cited evidence"
                )

        actual_by_window = {
            window_id: [
                utterance_id
                for utterance_id, utterance in utterance_map.items()
                if window_id in utterance["source_window_ids"]
            ]
            for window_id in allowed_window_ids
        }
        for decision in decisions:
            window_id = str(decision["window_id"])
            status = decision.get("status")
            if status not in WINDOW_STATUSES:
                raise ValueError(f"Invalid decision status for {window_id}")
            referenced = decision.get("source_utterance_ids", [])
            if not isinstance(referenced, list) or len(referenced) != len(
                set(referenced)
            ):
                raise ValueError(
                    f"Invalid source_utterance_ids for decision {window_id}"
                )
            if referenced != actual_by_window[window_id]:
                raise ValueError(
                    f"Decision {window_id} must list every utterance grounded in that window"
                )
            if status == "resolved" and not referenced:
                raise ValueError(f"Resolved decision {window_id} requires an utterance")
            if status == "resolved" and any(
                utterance_map[item]["language"] == "uncertain" for item in referenced
            ):
                raise ValueError(
                    f"Resolved decision {window_id} cannot cite uncertain utterances"
                )
            if status == "non_speech" and referenced:
                raise ValueError(
                    f"Non-speech decision {window_id} cannot cite utterances"
                )
            if status == "uncertain" and not str(decision.get("notes", "")).strip():
                raise ValueError(f"Uncertain decision {window_id} requires notes")

        previous_caption_start = -1.0
        captioned_utterance_ids: set[str] = set()
        for caption in captions:
            caption_id = str(caption.get("caption_id", "")).strip()
            if not caption_id or caption_id in all_caption_ids:
                raise ValueError("Caption IDs must be present and globally unique")
            all_caption_ids.add(caption_id)
            if not str(caption.get("display_text", "")).strip():
                raise ValueError(f"Caption {caption_id} has no display_text")
            if caption.get("language") not in CAPTION_LANGUAGES:
                raise ValueError(f"Caption {caption_id} has an invalid language")
            if caption.get("edit_type") not in EDIT_TYPES:
                raise ValueError(f"Caption {caption_id} has an invalid edit_type")
            if caption.get("review_status") not in REVIEW_STATUSES:
                raise ValueError(f"Caption {caption_id} has an invalid review_status")
            _validate_confidence(caption, f"Caption {caption_id}")
            source_utterance_ids = _require_unique_strings(
                caption.get("source_utterance_ids"),
                f"Caption {caption_id} source_utterance_ids",
            )
            invalid_utterances = set(source_utterance_ids) - set(utterance_map)
            if invalid_utterances:
                raise ValueError(
                    f"Caption {caption_id} cites invalid utterance IDs: "
                    + ", ".join(sorted(invalid_utterances))
                )
            source_utterances = [utterance_map[item] for item in source_utterance_ids]
            if any(item["language"] == "uncertain" for item in source_utterances):
                raise ValueError(
                    f"Caption {caption_id} cannot use uncertain utterances"
                )
            expected_window_ids = list(
                dict.fromkeys(
                    window_id
                    for item in source_utterances
                    for window_id in item["source_window_ids"]
                )
            )
            expected_candidate_ids = list(
                dict.fromkeys(
                    candidate_id
                    for item in source_utterances
                    for candidate_id in item["source_candidate_ids"]
                )
            )
            source_window_ids = _require_unique_strings(
                caption.get("source_window_ids"),
                f"Caption {caption_id} source_window_ids",
            )
            source_candidate_ids = _require_unique_strings(
                caption.get("source_candidate_ids"),
                f"Caption {caption_id} source_candidate_ids",
            )
            if set(source_window_ids) != set(expected_window_ids):
                raise ValueError(
                    f"Caption {caption_id} window IDs do not match sources"
                )
            if set(source_candidate_ids) != set(expected_candidate_ids):
                raise ValueError(
                    f"Caption {caption_id} candidate IDs do not match sources"
                )
            languages = {item["language"] for item in source_utterances}
            expected_language = (
                next(iter(languages)) if len(languages) == 1 else "mixed"
            )
            if caption["language"] != expected_language:
                raise ValueError(
                    f"Caption {caption_id} language does not match sources"
                )
            start = float(caption["start"])
            end = float(caption["end"])
            if start < 0 or end <= start or end > duration + EPSILON:
                raise ValueError(f"Caption {caption_id} is outside media duration")
            if start < previous_caption_start:
                raise ValueError(f"Captions in scene {group_id} must be chronological")
            previous_caption_start = start
            source_start = min(float(item["start"]) for item in source_utterances)
            source_end = max(float(item["end"]) for item in source_utterances)
            if (
                start < max(0.0, source_start - CAPTION_LEAD_SECONDS) - EPSILON
                or end > min(duration, source_end + CAPTION_TAIL_SECONDS) + EPSILON
                or _overlap(start, end, source_start, source_end) <= 0
            ):
                raise ValueError(
                    f"Caption {caption_id} timing is not grounded in sources"
                )
            if any(
                _overlap(
                    start,
                    end,
                    float(source_utterance["start"]),
                    float(source_utterance["end"]),
                )
                <= 0
                for source_utterance in source_utterances
            ):
                raise ValueError(
                    f"Caption {caption_id} does not overlap every cited source utterance"
                )
            captioned_utterance_ids.update(source_utterance_ids)

        lexical_ids = {
            utterance_id
            for utterance_id, utterance in utterance_map.items()
            if utterance["language"] in CAPTION_LANGUAGES
        }
        missing_captions = sorted(lexical_ids - captioned_utterance_ids)
        if missing_captions:
            raise ValueError(
                f"Scene {group_id} has resolved utterances without captions: "
                + ", ".join(missing_captions)
            )
        if packet.get("policy", {}).get("editorial_beats_required"):
            _validate_integrated_scene_review(
                packet_scene,
                reviewed_scene,
                utterance_map,
                captions,
                duration,
                bool(
                    packet.get("policy", {}).get(
                        "boundary_proposals_available", False
                    )
                ),
                bool(
                    packet.get("policy", {}).get(
                        "visual_moments_available", False
                    )
                ),
            )
            for beat in reviewed_scene["editorial_beats"]:
                if packet.get("policy", {}).get("clip_evidence_required") or "clip_evidence" in beat:
                    validate_clip_evidence(beat, packet_scene, utterance_map,
                                           {c["caption_id"]: c for c in captions})
            scene_beat_ids = {
                str(beat["beat_id"]) for beat in reviewed_scene["editorial_beats"]
            }
            if scene_beat_ids & all_beat_ids:
                raise ValueError("Editorial beat IDs must be globally unique")
            all_beat_ids.update(scene_beat_ids)


def build_no_candidate_scene_dialogue_review(
    packet_path: Path,
    output_path: Path,
) -> Path:
    packet_path = packet_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path == packet_path:
        raise ValueError("No-candidate review output must be a new file")
    packet = load_json(packet_path)
    validate_scene_dialogue_packet(packet)
    if packet.get("policy", {}).get("editorial_beats_required"):
        raise ValueError(
            "Integrated visual packets still require a model-authored scene review"
        )
    window_count = sum(len(scene["window_ids"]) for scene in packet["scenes"])
    if window_count:
        raise ValueError(
            "Deterministic no-candidate review is only valid when the packet has zero windows"
        )
    review = {
        "schema_version": SCENE_DIALOGUE_REVIEW_SCHEMA,
        "reviewer": "deterministic-no-apple-candidates",
        "method": "zero-window deterministic review",
        "notes": "No Apple candidates or evidence windows were available.",
        "scenes": [
            {
                "group_id": scene["group_id"],
                "window_decisions": [],
                "utterances": [],
                "captions": [],
            }
            for scene in packet["scenes"]
        ],
    }
    validate_scene_dialogue_review(packet, review)
    atomic_json(output_path, review)
    return output_path


def _slice_packet(packet: dict[str, Any], group_ids: list[str]) -> dict[str, Any]:
    validate_scene_dialogue_packet(packet)
    if not group_ids or len(group_ids) != len(set(group_ids)):
        raise ValueError("Scene dialogue slice requires unique group IDs")
    requested = set(group_ids)
    available = {str(scene["group_id"]) for scene in packet["scenes"]}
    missing = requested - available
    if missing:
        raise ValueError(
            "Unknown scene dialogue group IDs: " + ", ".join(sorted(missing))
        )
    scenes = [
        copy.deepcopy(scene)
        for scene in packet["scenes"]
        if str(scene["group_id"]) in requested
    ]
    sliced = copy.deepcopy(packet)
    sliced["scenes"] = scenes
    sliced_boundary_ids = {
        str(proposal["proposal_id"])
        for scene in scenes
        for proposal in (
            list(
                scene.get("boundary_context", {}).get("candidate_proposals", [])
            )
            + [
                scene.get("boundary_context", {}).get(key)
                for key in ("previous_proposal", "next_proposal")
                if scene.get("boundary_context", {}).get(key) is not None
            ]
        )
    }
    sliced_visual_moments = [
        moment
        for scene in scenes
        for moment in scene.get("visual_context", {}).get("visual_moments", [])
    ]
    boundary_proposals_available = bool(sliced_boundary_ids)
    visual_moments_available = bool(sliced_visual_moments)
    sliced.setdefault("policy", {})["boundary_proposals_available"] = (
        boundary_proposals_available
    )
    sliced["policy"]["visual_moments_available"] = visual_moments_available
    sliced["review_contract"] = _review_contract(
        clip_evidence_required=bool(sliced["policy"].get("clip_evidence_required")),
        integrated_visual_review=bool(
            sliced["policy"].get("integrated_visual_review", False)
        ),
        boundary_proposals_available=boundary_proposals_available,
        visual_moments_available=visual_moments_available,
    )
    sliced["summary"] = {
        "scene_count": len(scenes),
        "window_count": sum(len(scene["window_ids"]) for scene in scenes),
        "evidence_span_count": sum(
            len(candidate.get("evidence_spans", []))
            for scene in scenes
            for window in scene["windows"]
            for candidate in window.get("apple_candidates", {}).values()
        ),
        "boundary_proposal_count": len(sliced_boundary_ids),
        "visual_moment_count": len(sliced_visual_moments),
        "speech_free_visual_moment_count": sum(
            1 for moment in sliced_visual_moments if moment.get("speech_free") is True
        ),
    }
    if isinstance(sliced.get("boundary_evidence"), dict):
        sliced["boundary_evidence"]["proposal_count"] = len(sliced_boundary_ids)
    sliced["packet_slice"] = {
        "parent_asset_id": packet.get("asset_id"),
        "parent_scene_count": len(packet["scenes"]),
        "group_ids": [str(scene["group_id"]) for scene in scenes],
    }
    validate_scene_dialogue_packet(sliced)
    return sliced


def slice_scene_dialogue_packet(
    packet_path: Path,
    group_ids: list[str],
    output_path: Path,
) -> Path:
    packet_path = packet_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path == packet_path:
        raise ValueError("Scene dialogue slice output must be a new file")
    atomic_json(output_path, _slice_packet(load_json(packet_path), group_ids))
    return output_path


def _namespace_scene_review_ids(scene: dict[str, Any]) -> dict[str, Any]:
    """Make shard-local model IDs globally unique while preserving stable IDs."""
    result = copy.deepcopy(scene)
    group_id = str(result["group_id"])
    prefix = f"{group_id}-"
    utterance_id_map: dict[str, str] = {}
    for utterance in result.get("utterances", []):
        old_id = str(utterance["utterance_id"])
        new_id = old_id if old_id.startswith(prefix) else prefix + old_id
        utterance_id_map[old_id] = new_id
        utterance["utterance_id"] = new_id
    for decision in result.get("window_decisions", []):
        decision["source_utterance_ids"] = [
            utterance_id_map.get(str(item), str(item))
            for item in decision.get("source_utterance_ids", [])
        ]
    caption_id_map: dict[str, str] = {}
    for caption in result.get("captions", []):
        old_caption_id = str(caption["caption_id"])
        new_caption_id = (
            old_caption_id
            if old_caption_id.startswith(prefix)
            else prefix + old_caption_id
        )
        caption_id_map[old_caption_id] = new_caption_id
        caption["caption_id"] = new_caption_id
        caption["source_utterance_ids"] = [
            utterance_id_map.get(str(item), str(item))
            for item in caption.get("source_utterance_ids", [])
        ]
    for beat in result.get("editorial_beats", []):
        namespace_clip_evidence(beat, utterance_id_map)
        beat_id = str(beat["beat_id"])
        if not beat_id.startswith(prefix):
            beat["beat_id"] = prefix + beat_id
        beat["source_utterance_ids"] = [
            utterance_id_map.get(str(item), str(item))
            for item in beat.get("source_utterance_ids", [])
        ]
        beat["source_caption_ids"] = [
            caption_id_map.get(str(item), str(item))
            for item in beat.get("source_caption_ids", [])
        ]
    return result


def merge_scene_dialogue_review_shards(
    packet_path: Path,
    review_paths: list[Path],
    output_path: Path,
) -> Path:
    packet_path = packet_path.expanduser().resolve()
    review_paths = [path.expanduser().resolve() for path in review_paths]
    output_path = output_path.expanduser().resolve()
    if not review_paths:
        raise ValueError("At least one scene dialogue review shard is required")
    if output_path in {packet_path, *review_paths}:
        raise ValueError("Merged scene dialogue review output must be a new file")

    packet = load_json(packet_path)
    validate_scene_dialogue_packet(packet)
    expected_ids = [str(scene["group_id"]) for scene in packet["scenes"]]
    scenes_by_id: dict[str, dict[str, Any]] = {}
    reviewers: list[str] = []
    methods: list[str] = []
    notes: list[str] = []
    for review_path in review_paths:
        review = load_json(review_path)
        shard_scenes = review.get("scenes")
        if not isinstance(shard_scenes, list) or not shard_scenes:
            raise ValueError(f"Review shard has no scenes: {review_path}")
        group_ids = [str(scene.get("group_id", "")) for scene in shard_scenes]
        if any(not group_id for group_id in group_ids):
            raise ValueError(f"Review shard has a missing group ID: {review_path}")
        duplicate = set(group_ids) & set(scenes_by_id)
        if duplicate:
            raise ValueError(
                "Duplicate reviewed scene groups: " + ", ".join(sorted(duplicate))
            )
        validate_scene_dialogue_review(_slice_packet(packet, group_ids), review)
        scenes_by_id.update(
            (str(scene["group_id"]), _namespace_scene_review_ids(scene))
            for scene in shard_scenes
        )
        reviewers.append(str(review.get("reviewer", "")).strip())
        if str(review.get("method", "")).strip():
            methods.append(str(review["method"]).strip())
        if str(review.get("notes", "")).strip():
            notes.append(str(review["notes"]).strip())

    missing = [group_id for group_id in expected_ids if group_id not in scenes_by_id]
    extra = sorted(set(scenes_by_id) - set(expected_ids))
    if missing or extra:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ValueError(
            "Review shards do not cover the full packet: " + "; ".join(details)
        )

    merged = {
        "schema_version": SCENE_DIALOGUE_REVIEW_SCHEMA,
        "reviewer": " + ".join(dict.fromkeys(reviewers)),
        "method": "merged validated scene-dialogue review shards",
        "notes": " | ".join(dict.fromkeys(notes + methods)),
        "scenes": [scenes_by_id[group_id] for group_id in expected_ids],
    }
    validate_scene_dialogue_review(packet, merged)
    atomic_json(output_path, merged)
    return output_path


def _attached_copy(item: dict[str, Any], start: float, end: float) -> dict[str, Any]:
    source_start = float(item["start"])
    source_end = float(item["end"])
    return {
        **copy.deepcopy(item),
        "source_start": source_start,
        "source_end": source_end,
        "overlap_start": max(source_start, start),
        "overlap_end": min(source_end, end),
    }


def _dialogue_preservation_audit(
    packet: dict[str, Any],
    review: dict[str, Any],
    utterances: list[dict[str, Any]],
    captions: list[dict[str, Any]],
) -> dict[str, Any]:
    captioned_utterance_ids = {
        str(utterance_id)
        for caption in captions
        for utterance_id in caption.get("source_utterance_ids", [])
    }
    lexical_utterance_ids = {
        str(utterance["utterance_id"])
        for utterance in utterances
        if utterance.get("language") in CAPTION_LANGUAGES
    }
    uncaptioned_lexical_ids = sorted(lexical_utterance_ids - captioned_utterance_ids)
    status_counts = {status: 0 for status in sorted(WINDOW_STATUSES)}
    uncertain_windows_with_captions = 0
    for scene in review["scenes"]:
        for decision in scene["window_decisions"]:
            status = str(decision["status"])
            status_counts[status] += 1
            if status == "uncertain" and any(
                str(utterance_id) in captioned_utterance_ids
                for utterance_id in decision.get("source_utterance_ids", [])
            ):
                uncertain_windows_with_captions += 1

    lexical_count = len(lexical_utterance_ids)
    captioned_lexical_count = len(lexical_utterance_ids & captioned_utterance_ids)
    return {
        "policy_version": packet.get("policy", {}).get(
            "policy_version", "legacy-unversioned"
        ),
        "status": "pass" if not uncaptioned_lexical_ids else "fail",
        "window_status_counts": status_counts,
        "lexical_utterance_count": lexical_count,
        "captioned_lexical_utterance_count": captioned_lexical_count,
        "uncaptioned_lexical_utterance_ids": uncaptioned_lexical_ids,
        "caption_coverage_ratio": (
            round(captioned_lexical_count / lexical_count, 6) if lexical_count else 1.0
        ),
        "uncertain_windows_with_captioned_speech": (uncertain_windows_with_captions),
        "uncertain_residue_utterance_count": sum(
            utterance.get("language") == "uncertain" for utterance in utterances
        ),
    }


def merge_scene_dialogue_review(
    timeline_path: Path,
    packet_path: Path,
    review_path: Path,
    output_path: Path,
) -> Path:
    timeline_path = timeline_path.expanduser().resolve()
    packet_path = packet_path.expanduser().resolve()
    review_path = review_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path in {timeline_path, packet_path, review_path}:
        raise ValueError("Reviewed dialogue timeline output must be a new file")
    timeline = load_json(timeline_path)
    packet = load_json(packet_path)
    review = load_json(review_path)
    validate_scene_dialogue_packet(packet, timeline)
    validate_scene_dialogue_review(packet, review)

    result = copy.deepcopy(timeline)
    if not isinstance(result.get("context_groups"), list):
        result["schema_version"] = "phase1-context-reviewed-timeline/v1"
        result["context_groups"] = _timeline_groups(timeline)
    # Integrated review may promote a visual-moment frame to the canonical scene
    # representative.  Those frames originate outside Phase 1's sparse sample list,
    # so retain them as timeline samples before downstream web/library renderers
    # resolve representative_sample_id.
    known_sample_ids = {
        str(sample.get("sample_id", "")) for sample in result.get("samples", [])
    }
    for packet_scene in packet["scenes"]:
        for frame in packet_scene.get("visual_context", {}).get(
            "candidate_frames", []
        ):
            sample_id = str(frame.get("sample_id", ""))
            if (
                sample_id
                and sample_id not in known_sample_ids
                and frame.get("visual_moment_id")
            ):
                result["samples"].append(copy.deepcopy(frame))
                known_sample_ids.add(sample_id)
    result["samples"].sort(key=lambda sample: float(sample["time"]))
    reviewed_by_group = {scene["group_id"]: scene for scene in review["scenes"]}
    utterances: list[dict[str, Any]] = []
    captions: list[dict[str, Any]] = []
    editorial_beats: list[dict[str, Any]] = []
    for packet_scene in packet["scenes"]:
        group_id = packet_scene["group_id"]
        reviewed_scene = reviewed_by_group[group_id]
        for utterance in reviewed_scene["utterances"]:
            utterances.append(
                {
                    **copy.deepcopy(utterance),
                    "group_id": group_id,
                    "start_timecode": format_time(float(utterance["start"])),
                    "end_timecode": format_time(float(utterance["end"])),
                }
            )
        for caption in reviewed_scene["captions"]:
            captions.append(
                {
                    **copy.deepcopy(caption),
                    "group_id": group_id,
                    "start_timecode": format_time(float(caption["start"])),
                    "end_timecode": format_time(float(caption["end"])),
                }
            )
        for beat in reviewed_scene.get("editorial_beats", []):
            editorial_beats.append(
                {
                    **copy.deepcopy(beat),
                    "group_id": group_id,
                    "start_timecode": format_time(float(beat["start"])),
                    "end_timecode": format_time(float(beat["end"])),
                }
            )

    group_map = {group["group_id"]: group for group in result["context_groups"]}
    for packet_scene in packet["scenes"]:
        group_id = packet_scene["group_id"]
        reviewed_scene = reviewed_by_group[group_id]
        group = group_map[group_id]
        if "person_observations" in packet_scene:
            group["person_observations"] = copy.deepcopy(packet_scene["person_observations"])
        group["reviewed_dialogue"] = {
            "window_decisions": copy.deepcopy(reviewed_scene["window_decisions"]),
            "utterances": [
                _attached_copy(item, float(group["start"]), float(group["end"]))
                for item in utterances
                if _overlap(
                    float(group["start"]),
                    float(group["end"]),
                    float(item["start"]),
                    float(item["end"]),
                )
                > 0
            ],
            "captions": [
                _attached_copy(item, float(group["start"]), float(group["end"]))
                for item in captions
                if _overlap(
                    float(group["start"]),
                    float(group["end"]),
                    float(item["start"]),
                    float(item["end"]),
                )
                > 0
            ],
        }
        if "scene_understanding" in reviewed_scene:
            understanding = copy.deepcopy(reviewed_scene["scene_understanding"])
            prior_context_review = group.get("context_review", {})
            group["scene_understanding"] = understanding
            dialogue_summaries = [
                str(beat["summary"])
                for beat in reviewed_scene.get("editorial_beats", [])
                if beat.get("source_caption_ids")
            ]
            group["context_review"] = {
                "group_id": group_id,
                "narrative_summary": understanding["narrative_summary"],
                "dialogue_summary": " ".join(dialogue_summaries),
                "dialogue_evidence": sorted(
                    {
                        utterance["language"]
                        for utterance in reviewed_scene["utterances"]
                        if utterance["language"] != "uncertain"
                    }
                ),
                "representative_sample_id": understanding["representative_sample_id"],
                "representative_reason": (
                    "통합 화면·대화 리뷰에서 장면을 대표하는 프레임으로 선택"
                ),
                "key_moments": copy.deepcopy(
                    prior_context_review.get("key_moments", [])
                ),
                "notable_moments": copy.deepcopy(
                    understanding.get("notable_moments", [])
                ),
                "confidence": understanding["confidence"],
            }
        if "editorial_beats" in reviewed_scene:
            group["editorial_beats"] = copy.deepcopy(
                [item for item in editorial_beats if item["group_id"] == group_id]
            )

    for segment in result.get("segments", []):
        start = float(segment["start"])
        end = float(segment["end"])
        segment["reviewed_utterances"] = [
            _attached_copy(item, start, end)
            for item in utterances
            if _overlap(start, end, float(item["start"]), float(item["end"])) > 0
        ]
        segment["caption_lines"] = [
            _attached_copy(item, start, end)
            for item in captions
            if _overlap(start, end, float(item["start"]), float(item["end"])) > 0
        ]

    result["reviewed_dialogue"] = {
        "schema_version": REVIEWED_DIALOGUE_SCHEMA,
        "inputs": {
            "timeline": str(timeline_path),
            "packet": str(packet_path),
            "review": str(review_path),
            **(
                {"boundary_proposals": packet["inputs"]["boundary_proposals"]}
                if packet.get("inputs", {}).get("boundary_proposals")
                else {}
            ),
        },
        "policy": copy.deepcopy(packet.get("policy", {})),
        "policy_audit": _dialogue_preservation_audit(
            packet,
            review,
            utterances,
            captions,
        ),
        "summary": {
            "scene_count": len(packet["scenes"]),
            "window_count": sum(len(scene["window_ids"]) for scene in packet["scenes"]),
            "utterance_count": len(utterances),
            "caption_count": len(captions),
            "uncertain_utterance_count": sum(
                item["language"] == "uncertain" for item in utterances
            ),
            **(
                {"editorial_beat_count": len(editorial_beats)}
                if packet.get("policy", {}).get("editorial_beats_required")
                else {}
            ),
        },
        "utterances": utterances,
        "captions": captions,
        **(
            {"editorial_beats": editorial_beats}
            if packet.get("policy", {}).get("editorial_beats_required")
            else {}
        ),
        "attachment": {
            "schema_version": REVIEWED_DIALOGUE_ATTACHMENT_SCHEMA,
            "source_timebase": "seconds_from_source_start",
            "authoritative_caption_source": True,
            "reviewer": review.get("reviewer"),
            "method": review.get("method"),
            "notes": review.get("notes"),
        },
    }
    atomic_json(output_path, result)
    return output_path
