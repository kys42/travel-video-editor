"""Versioned model interpretation and deterministic, source-linked clip candidates.

Machine observations never establish identity or absence between sampled frames.
This module deliberately does not rank clips or silently approve privacy policies.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .library_audit import audit_library
from .phase1 import atomic_json
from .review import load_json

SCHEMA = "clip-editorial-evidence/v1"
CATALOG_SCHEMA = "clip-candidate-library/v1"
PERSON_SCHEMA = "sampled-person-observations/v1"
EPS = 0.05


def contract() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "required": [
            "schema_version",
            "core_range",
            "event_closure",
            "steps",
            "subjects",
            "people",
            "speakers",
            "interactions",
            "audio",
            "quality",
        ],
        "range": "core_range: {start,end}; within beat, anchored to beat edges, supplied frame times or cited utterance/caption bounds; never cut a cited utterance or caption",
        "event_closure": ["closed", "open", "unknown"],
        "claim": {
            "required": [
                "kind",
                "description",
                "start",
                "end",
                "basis",
                "source_sample_ids",
                "source_utterance_ids",
                "confidence",
            ],
            "basis": ["visual", "speech", "inferred"],
            "rule": "visual needs in-range frame evidence; speech needs overlapping reviewed utterances; inferred is explicitly contextual, never direct observation",
        },
        "steps": "claim[]; kind: setup/action/reaction/payoff/transition. Retain narrative order, use [] when unsupported.",
        "subjects": "claim[]; kind: wildlife/place/food/activity/object. Separate visible subjects from spoken mentions.",
        "people": "[{person_id, role, basis, source_sample_ids, source_detection_ids, confidence}]; role unknown/traveler/staff/guide/bystander, basis visual/inferred; IDs local to beat. Role except unknown requires inferred. Record clearly visible editorially relevant anonymous people with role unknown even when identity and speaker links are unknown. Never infer owner identity or cross-video identity. Detections and cited frames must coincide within sampling interval.",
        "speakers": "[{speaker_id, source_utterance_ids, person_id, link_basis, confidence}]; person_id null with link_basis unknown, or a local person ID with link_basis inferred. STT locale is NOT speaker identity; leave [] or unknown when voice separation lacks evidence.",
        "interactions": "claim[] plus participant_ids (local person/speaker IDs); kind order/guidance/question_answer/conversation/other. Needs utterance evidence; do not invent answers.",
        "audio": "claim[]; kind speech/laughter/music/crowd/nature/mechanical/noise. This packet supplies frames and STT, not verified environmental listening: speech may use basis speech; all other audio must be inferred, or omit. Do not infer non-speech audio from visual presence as observed fact.",
        "quality": "claim[]; kind stable/shaky/occluded/blurred/exposure_issue. Only sampled visual evidence or explicit inference; no whole-interval quality guarantee from a frame.",
        "unknown_policy": "Empty lists mean unassessed, never absent. Face-free clearance and owner mapping are separate human/selected-range verification. No readiness or privacy approval authored by the model.",
    }


def _number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _confidence(item: dict) -> None:
    if not 0 <= _number(item.get("confidence"), "confidence") <= 1:
        raise ValueError("confidence must be between 0 and 1")


def _ids(value: Any, allowed: dict | set, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(x, str) or not x for x in value
    ):
        raise ValueError(f"{label} must be a list of IDs")
    if len(value) != len(set(value)) or set(value) - set(allowed):
        raise ValueError(f"{label} contains duplicate or unknown IDs")
    return value


def _range(item: dict, start: float, end: float) -> tuple[float, float]:
    a, b = _number(item.get("start"), "start"), _number(item.get("end"), "end")
    if a < start - EPS or b > end + EPS or b <= a:
        raise ValueError("clip evidence range is outside its beat")
    return a, b


def validate_person_observations(payload: dict, duration: float) -> None:
    if (
        payload.get("schema_version") != PERSON_SCHEMA
        or payload.get("coverage") != "sampled_only"
    ):
        raise ValueError("Unsupported sampled person observations")
    if _number(payload.get("sample_interval_seconds"), "sample interval") <= 0:
        raise ValueError("Invalid person sample interval")
    samples = payload.get("observations")
    if not isinstance(samples, list):
        raise ValueError("Person observations must be a list")
    seen = set()
    for item in samples:
        oid = item.get("observation_id")
        if not isinstance(oid, str) or not oid or oid in seen:
            raise ValueError("Person observation IDs must be unique")
        seen.add(oid)
        if item.get("kind") not in {"face", "person"}:
            raise ValueError("Invalid person observation kind")
        if not 0 <= _number(item.get("timestamp"), "timestamp") <= duration:
            raise ValueError("Person observation is outside source duration")
        _confidence(item)
        box = item.get("bounding_box")
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError("Person bounding box must be [x,y,width,height]")
        x, y, w, h = [_number(n, "bounding box") for n in box]
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1.00001 or y + h > 1.00001:
            raise ValueError("Person bounding box must be normalized")


def person_observations(raw: dict, duration: float) -> dict:
    """Keep positive detections at actual observation times, without tracking/absence inference."""
    observations = []
    for kind, key in (("face", "faceObservations"), ("person", "personObservations")):
        for index, item in enumerate(raw.get(key, []), 1):
            # Vision may predict boxes extending beyond the image for partial faces.
            # Preserve the original in raw JSON; expose its visible intersection here.
            x, y, w, h = [
                _number(n, "Vision bounding box") for n in item["boundingBox"]
            ]
            if w <= 0 or h <= 0:
                raise ValueError("Invalid Vision bounding box size")
            left, bottom, right, top = (
                max(0, x),
                max(0, y),
                min(1, x + w),
                min(1, y + h),
            )
            if right <= left or top <= bottom:
                raise ValueError("Vision bounding box does not intersect the image")
            observations.append(
                {
                    "observation_id": f"PV-{kind}-{index:06d}",
                    "kind": kind,
                    "timestamp": item["timestamp"],
                    "bounding_box": [left, bottom, right - left, top - bottom],
                    "frame_edge_truncated": x < 0 or y < 0 or x + w > 1 or y + h > 1,
                    "confidence": item["confidence"],
                }
            )
    result = {
        "schema_version": PERSON_SCHEMA,
        "coverage": "sampled_only",
        "sample_interval_seconds": raw["visualIntervalSeconds"],
        "coordinates": "normalized_lower_left_origin",
        "status": "available"
        if "faceObservations" in raw and "personObservations" in raw
        else "legacy_counts_only",
        "observations": sorted(
            observations, key=lambda item: (item["timestamp"], item["observation_id"])
        ),
    }
    validate_person_observations(result, duration)
    return result


def validate_clip_evidence(
    beat: dict, scene: dict, utterances: dict, captions: dict
) -> None:
    data = beat.get("clip_evidence")
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA:
        raise ValueError(f"Beat {beat.get('beat_id')} requires {SCHEMA}")
    if set(contract()["required"]) - set(data):
        raise ValueError("Clip evidence is missing required fields")
    start, end = _number(beat["start"], "beat start"), _number(beat["end"], "beat end")
    frames = {
        str(f["sample_id"]): f
        for f in scene.get("visual_context", {}).get("candidate_frames", [])
    }
    person_packet = scene.get("person_observations", {})
    detections = {p["observation_id"]: p for p in person_packet.get("observations", [])}
    # Restrict references to the owning beat's speech, not unrelated neighboring turns.
    speech = {uid: utterances[uid] for uid in beat.get("source_utterance_ids", [])}
    a, b = _range(data["core_range"], start, end)
    anchors = {start, end, *(float(f["time"]) for f in frames.values())}
    for item in [
        *speech.values(),
        *(captions[cid] for cid in beat.get("source_caption_ids", [])),
    ]:
        anchors.update((float(item["start"]), float(item["end"])))
        if max(a, float(item["start"])) < min(b, float(item["end"])):
            if a > float(item["start"]) + EPS or b < float(item["end"]) - EPS:
                raise ValueError("Core range cuts an utterance or caption")
    if any(not any(abs(t - anchor) <= EPS for anchor in anchors) for t in (a, b)):
        raise ValueError("Core range must use evidence anchors")
    if data["event_closure"] not in {"closed", "open", "unknown"}:
        raise ValueError("Invalid event closure")

    def claim(item: dict, kinds: set) -> None:
        if (
            not isinstance(item, dict)
            or item.get("kind") not in kinds
            or not isinstance(item.get("description"), str)
            or not item["description"].strip()
        ):
            raise ValueError("Invalid clip evidence claim")
        x, y = _range(item, start, end)
        _confidence(item)
        fs = _ids(item.get("source_sample_ids"), frames, "claim frames")
        us = _ids(item.get("source_utterance_ids"), speech, "claim utterances")
        if any(not x - EPS <= float(frames[f]["time"]) <= y + EPS for f in fs):
            raise ValueError("Claim frame is outside claim range")
        if any(
            max(x, float(speech[u]["start"])) >= min(y, float(speech[u]["end"]))
            for u in us
        ):
            raise ValueError("Claim utterance does not overlap its range")
        basis = item.get("basis")
        if basis not in {"visual", "speech", "inferred"} or not (fs or us):
            raise ValueError("Claim requires explicit basis and evidence")
        if basis == "visual" and not fs or basis == "speech" and not us:
            raise ValueError("Claim basis lacks matching modality evidence")

    kinds = {
        "steps": {"setup", "action", "reaction", "payoff", "transition"},
        "subjects": {"wildlife", "place", "food", "activity", "object"},
        "interactions": {
            "order",
            "guidance",
            "question_answer",
            "conversation",
            "other",
        },
        "audio": {
            "speech",
            "laughter",
            "music",
            "crowd",
            "nature",
            "mechanical",
            "noise",
        },
        "quality": {"stable", "shaky", "occluded", "blurred", "exposure_issue"},
    }
    for field in [*kinds, "people", "speakers"]:
        if not isinstance(data[field], list):
            raise ValueError(f"{field} must be a list (empty means unknown)")
    for field, allowed in kinds.items():
        for item in data[field]:
            claim(item, allowed)
            if field == "audio" and (
                item["basis"] == "visual"
                or (item["kind"] != "speech" and item["basis"] != "inferred")
            ):
                raise ValueError(
                    "Environmental audio needs listening evidence; this packet only supports inference"
                )
            if field == "quality" and item["basis"] == "speech":
                raise ValueError("Visual quality cannot be observed from speech")
    if [s["start"] for s in data["steps"]] != sorted(s["start"] for s in data["steps"]):
        raise ValueError("Event steps must be chronological")
    people = {}
    for person in data["people"]:
        pid = person.get("person_id")
        if not isinstance(pid, str) or not pid or pid in people:
            raise ValueError("Person IDs must be unique within beat")
        people[pid] = person
        if person.get("role") not in {
            "unknown",
            "traveler",
            "staff",
            "guide",
            "bystander",
        }:
            raise ValueError("Invalid person role; owner identity cannot be inferred")
        if person.get("basis") not in {"visual", "inferred"} or (
            person["role"] != "unknown" and person["basis"] != "inferred"
        ):
            raise ValueError("Person role requires explicit inference")
        _confidence(person)
        fs = _ids(person.get("source_sample_ids"), frames, "person frames")
        ds = _ids(person.get("source_detection_ids"), detections, "person detections")
        if not fs or any(
            not start - EPS <= float(frames[f]["time"]) <= end + EPS for f in fs
        ):
            raise ValueError("Onscreen person requires in-beat visual frames")
        cadence = float(person_packet.get("sample_interval_seconds", 0))
        for did in ds:
            t = float(detections[did]["timestamp"])
            if not start - EPS <= t <= end + EPS or not any(
                abs(t - float(frames[f]["time"])) <= cadence + EPS for f in fs
            ):
                raise ValueError("Person detection is not near cited in-beat frame")
    speakers = {}
    for speaker in data["speakers"]:
        sid = speaker.get("speaker_id")
        if not isinstance(sid, str) or not sid or sid in speakers or sid in people:
            raise ValueError("Speaker IDs must be unique and distinct from person IDs")
        speakers[sid] = speaker
        _confidence(speaker)
        if not _ids(speaker.get("source_utterance_ids"), speech, "speaker utterances"):
            raise ValueError("Speaker requires utterance evidence")
        pid = speaker.get("person_id")
        if pid is None:
            if speaker.get("link_basis") != "unknown":
                raise ValueError("Unlinked speaker must retain unknown link")
        elif pid not in people or speaker.get("link_basis") != "inferred":
            raise ValueError(
                "Speaker/person link must be a local, explicitly inferred link"
            )
    for item in data["interactions"]:
        if not item["source_utterance_ids"] or not _ids(
            item.get("participant_ids"),
            set(people) | set(speakers),
            "interaction participants",
        ):
            raise ValueError("Interaction requires participants and speech evidence")


def namespace_clip_evidence(beat: dict, utterance_ids: dict[str, str]) -> None:
    data = beat.get("clip_evidence")
    if not data:
        return
    for field in ("steps", "subjects", "speakers", "interactions", "audio", "quality"):
        for item in data[field]:
            item["source_utterance_ids"] = [
                utterance_ids.get(x, x) for x in item["source_utterance_ids"]
            ]


def _dialogue_neighbors(items: list[dict], start: float, end: float) -> dict:
    """Snapshot nearby source speech even when it belongs to another coarse group."""
    ordered = sorted(items, key=lambda item: (float(item["start"]), float(item["end"])))
    return copy.deepcopy(
        {
            "overlapping": [
                item
                for item in ordered
                if float(item["start"]) < end and float(item["end"]) > start
            ],
            "previous": [item for item in ordered if float(item["end"]) <= start][-2:],
            "next": [item for item in ordered if float(item["start"]) >= end][:2],
        }
    )


def build_candidate_library(timeline: dict) -> dict:
    """Materialize candidates, not good-clip rankings or privacy clearances."""
    duration = _number(timeline.get("media", {}).get("duration"), "source duration")
    asset_id, source = timeline.get("asset_id"), timeline.get("source", {})
    if not asset_id or not source.get("path") or not source.get("quick_fingerprint"):
        raise ValueError("Candidate library requires source identity and fingerprint")
    dialogue = timeline.get("reviewed_dialogue", {})
    captions = {c["caption_id"]: c for c in dialogue.get("captions", [])}
    utterances = {u["utterance_id"]: u for u in dialogue.get("utterances", [])}
    beats = dialogue.get("editorial_beats")
    if beats is None:
        beats = [
            b
            for g in timeline.get("context_groups", [])
            for b in g.get("editorial_beats", [])
        ]
    groups = {g["group_id"]: g for g in timeline.get("context_groups", [])}
    records, seen = [], set()
    for beat in beats:
        bid = beat["beat_id"]
        if bid in seen:
            raise ValueError("Duplicate candidate beat ID")
        seen.add(bid)
        a, b = _range(beat, 0, duration)
        cids = _ids(beat.get("source_caption_ids", []), captions, "candidate captions")
        _ids(beat.get("source_utterance_ids", []), utterances, "candidate utterances")
        group = groups.get(beat.get("group_id"))
        if group is None:
            raise ValueError("Candidate has no owning context group")
        dialogue_context = {
            "captions": _dialogue_neighbors(list(captions.values()), a, b),
            "utterances": _dialogue_neighbors(list(utterances.values()), a, b),
        }
        context_items = [
            item
            for collection in dialogue_context.values()
            for entries in collection.values()
            for item in entries
        ]
        evidence = copy.deepcopy(beat.get("clip_evidence"))
        if evidence:
            validate_clip_evidence(
                beat,
                {
                    "visual_context": {"candidate_frames": timeline.get("samples", [])},
                    "person_observations": group.get("person_observations", {}),
                },
                utterances,
                captions,
            )
        reasons = []
        if not evidence:
            reasons.append("editorial_evidence_missing")
        elif evidence["event_closure"] != "closed":
            reasons.append("event_closure_unconfirmed")
        if beat.get("boundary_adjustment") != "none":
            reasons.append("neighbor_boundary_review")
        if beat.get("dialogue_closure") == "open":
            reasons.append("dialogue_open")
        if any(
            "…" in captions[cid]["display_text"]
            or "..." in captions[cid]["display_text"]
            for cid in cids
        ):
            # Preserve partial speech, but surface its uncertainty for clip assembly.
            reasons.append("caption_uncertainty")
        for item in [*captions.values(), *utterances.values()]:
            x, y = float(item["start"]), float(item["end"])
            if max(a, x) < min(b, y) and (x < a - EPS or y > b + EPS):
                reasons.append("speech_boundary_cut")
                break
        # Quality promotion requires separate review; a closed model event alone is insufficient.
        record = {
            "candidate_id": "C-"
            + hashlib.sha256(f"{asset_id}::{bid}".encode()).hexdigest()[:16],
            "asset_id": asset_id,
            "source": copy.deepcopy(source),
            "group_id": beat["group_id"],
            "beat_id": bid,
            "title": beat["title"],
            "summary": beat["summary"],
            "recommended_range": {"start": a, "end": b},
            "context_range": {
                "start": min(
                    a,
                    float(group["start"]),
                    *(float(item["start"]) for item in context_items),
                ),
                "end": max(
                    b,
                    float(group["end"]),
                    *(float(item["end"]) for item in context_items),
                ),
            },
            "core_range": evidence["core_range"] if evidence else None,
            "dialogue": " ".join(captions[c]["display_text"] for c in cids),
            "dialogue_context": dialogue_context,
            "evidence": evidence,
            "references": {
                k: copy.deepcopy(v)
                for k, v in beat.items()
                if k.startswith("source_") or k == "representative_sample_ids"
            },
            "readiness": "needs_review" if reasons else "structured_candidate",
            "review_reasons": sorted(set(reasons)),
            "privacy": {"owners": "unknown", "no_faces": "unknown"},
            "audio_policy": "unassessed",
        }
        record["revision"] = hashlib.sha256(
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16]
        records.append(record)
    return {
        "schema_version": CATALOG_SCHEMA,
        "asset_id": asset_id,
        "source": copy.deepcopy(source),
        "candidates": records,
        "summary": {"candidate_count": len(records), "model_calls": 0},
        "library_audit": audit_library(timeline),
    }


def export_candidate_library(timeline_path: Path, output_path: Path) -> Path:
    if timeline_path.resolve() == output_path.resolve():
        raise ValueError("Candidate export must not overwrite input timeline")
    atomic_json(output_path, build_candidate_library(load_json(timeline_path)))
    return output_path
