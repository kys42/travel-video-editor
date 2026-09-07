"""Cheap review signals for library granularity; never a semantic quality score."""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from typing import Any


def audit_library(timeline: dict[str, Any]) -> dict[str, Any]:
    groups = timeline.get("context_groups", [])
    records = []
    for group in groups:
        beats = group.get("editorial_beats", [])
        duration = float(group["end"]) - float(group["start"])
        description = str(group.get("context_review", {}).get("narrative_summary", ""))
        summaries = [str(b.get("summary", "")).strip() for b in beats]
        repeated = [s for s, n in Counter(summaries).items() if s and n > 1]
        flags = []
        # Advisory thresholds only: long continuous shots can legitimately remain intact.
        if len(beats) <= 1 and duration >= 30:
            flags.append("long_group_without_subdivision")
        if repeated:
            flags.append("repeated_beat_summary")
        if any(
            len(a) >= 60
            and len(b) >= 60
            and a != b
            and SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.85
            for a, b in zip(summaries, summaries[1:])
        ):
            flags.append("near_duplicate_beat_summary")
        shared_claims = Counter(
            text
            for beat in beats
            for text in {
                str(c.get("description", "")).strip()
                for key in ("steps", "subjects", "interactions")
                for c in (beat.get("clip_evidence") or {}).get(key, [])
            }
            if text
        )
        if any(n >= 3 and n > len(beats) / 2 for n in shared_claims.values()):
            flags.append("repeated_evidence_across_beats")
        if len(description.strip()) < 60 and duration >= 30:
            flags.append("short_scene_description")
        missing = {
            key: sum(not (b.get("clip_evidence") or {}).get(key) for b in beats)
            for key in (
                "steps",
                "subjects",
                "people",
                "interactions",
                "audio",
                "quality",
            )
        }
        records.append(
            {
                "group_id": group["group_id"],
                "duration": duration,
                "candidate_count": len(beats),
                "description_chars": len(description),
                "unassessed_candidate_fields": missing,
                "review_signals": flags,
            }
        )
    return {
        "schema_version": "scene-library-audit/v1",
        "status": "review_required"
        if any(r["review_signals"] for r in records)
        else "no_heuristic_flags",
        "notice": "Heuristics route review only. Counts, text length and empty fields do not establish semantic quality or absence; no automatic splitting or acceptance.",
        "summary": {
            "group_count": len(records),
            "candidate_count": sum(r["candidate_count"] for r in records),
            "single_candidate_groups": sum(r["candidate_count"] == 1 for r in records),
            "description_chars": sum(r["description_chars"] for r in records),
            "flagged_groups": sum(bool(r["review_signals"]) for r in records),
        },
        "groups": records,
    }
