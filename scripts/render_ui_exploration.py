from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _timecode(seconds: float) -> str:
    minutes, value = divmod(max(0, seconds), 60)
    return f"{int(minutes):02d}:{value:06.3f}"


def _uri(path: str | Path | None) -> str | None:
    if not path:
        return None
    resolved = Path(path).resolve()
    mime = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    payload = base64.b64encode(resolved.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def _capture_label(value: str | None) -> str:
    if not value:
        return "시간 미상"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%H:%M:%S")


def _sample_map(timeline: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {sample["sample_id"]: sample for sample in timeline["samples"]}


def _scene_segments(timeline: dict[str, Any], scene: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = set(scene.get("segment_ids", []))
    segments: list[dict[str, Any]] = []
    for segment in timeline.get("segments", []):
        if segment.get("segment_id") not in wanted:
            continue
        review = segment.get("review") or {}
        candidates = segment.get("transcript_candidates") or {}
        segments.append(
            {
                "id": segment["segment_id"],
                "start": segment["start"],
                "end": segment["end"],
                "time": _timecode(float(segment["start"])),
                "visual": review.get("visual_summary") or "장면 설명 없음",
                "actions": review.get("actions") or [],
                "importance": review.get("importance"),
                "confidence": review.get("confidence"),
                "preference": segment.get("transcript_machine_preference"),
                "transcripts": {
                    language: [item.get("text", "") for item in items if item.get("text")]
                    for language, items in candidates.items()
                },
            }
        )
    return segments


def _normalize_video(path: Path, timeline: dict[str, Any]) -> dict[str, Any]:
    summary = timeline["video_summary"]
    samples = _sample_map(timeline)
    representative = samples[summary["representative_sample_id"]]
    scenes: list[dict[str, Any]] = []
    events = {item["group_id"]: item for item in summary["chronological_events"]}
    highlights = set(summary.get("highlight_group_ids", []))

    for scene in timeline["context_groups"]:
        context = scene["context_review"]
        event = events.get(scene["group_id"], {})
        keyframes = []
        for item in context.get("key_moments", []):
            sample = samples.get(item["sample_id"])
            if not sample:
                continue
            keyframes.append(
                {
                    "id": sample["sample_id"],
                    "time": sample["time"],
                    "timecode": sample.get("timecode") or _timecode(float(sample["time"])),
                    "frame": _uri(sample["frame"]),
                    "role": item["role"],
                }
            )
        notables = []
        for item in context.get("notable_moments", []):
            sample = samples.get(item["sample_id"], {})
            notables.append(
                {
                    **item,
                    "time": sample.get("time"),
                    "timecode": sample.get("timecode") or _timecode(float(sample.get("time", scene["start"]))),
                    "frame": _uri(sample.get("frame")),
                }
            )
        rep = samples[context["representative_sample_id"]]
        scenes.append(
            {
                "id": scene["group_id"],
                "start": float(scene["start"]),
                "end": float(scene["end"]),
                "startTime": _timecode(float(scene["start"])),
                "endTime": _timecode(float(scene["end"])),
                "duration": float(scene["end"]) - float(scene["start"]),
                "label": scene["label"],
                "headline": event.get("headline") or scene["label"],
                "eventDescription": event.get("description") or context["narrative_summary"],
                "action": context["narrative_summary"],
                "dialogue": context.get("dialogue_summary") or "유효한 대화 없음",
                "languages": context.get("dialogue_evidence") or [],
                "confidence": context.get("confidence"),
                "highlight": scene["group_id"] in highlights,
                "representativeFrame": _uri(rep["frame"]),
                "representativeTime": rep.get("timecode") or _timecode(float(rep["time"])),
                "representativeReason": context.get("representative_reason"),
                "keyframes": keyframes,
                "notables": notables,
                "segments": _scene_segments(timeline, scene),
            }
        )

    detail_path = path.parent / "web" / "index.html"
    return {
        "id": timeline["asset_id"],
        "source": timeline["source"]["name"],
        "title": summary["title"],
        "oneLine": summary["one_line_summary"],
        "narrative": summary["narrative_summary"],
        "duration": float(timeline["media"]["duration"]),
        "durationLabel": _timecode(float(timeline["media"]["duration"])),
        "captureTime": _capture_label(timeline["media"].get("creation_time")),
        "sizeBytes": timeline["media"].get("size_bytes"),
        "codec": timeline["media"]["video"].get("codec", "—").upper(),
        "resolution": f'{timeline["media"]["video"].get("width", "—")}×{timeline["media"]["video"].get("height", "—")}',
        "tags": summary.get("tags", []),
        "representativeFrame": _uri(representative["frame"]),
        "representativeTime": representative.get("timecode") or _timecode(float(representative["time"])),
        "sceneCount": len(scenes),
        "notableCount": sum(len(scene["notables"]) for scene in scenes),
        "detailUrl": detail_path.resolve().as_uri() if detail_path.is_file() else None,
        "proxyUrl": None,
        "scenes": scenes,
    }


def render(manifest_path: Path, output_dir: Path) -> Path:
    manifest = _load(manifest_path.resolve())
    timelines = [Path(item).resolve() for item in manifest["timelines"]]
    videos = [_normalize_video(path, _load(path)) for path in timelines]
    source_dir = Path(__file__).resolve().parents[1] / "design" / "ui-exploration"
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "app.css", "app.js"):
        shutil.copy2(source_dir / name, output_dir / name)
    data = {
        "schemaVersion": "ui-exploration/v1",
        "projectTitle": manifest.get("title", "Travel video review"),
        "sourceManifest": str(manifest_path.resolve()),
        "videos": videos,
    }
    (output_dir / "data.js").write_text(
        "window.TRAVEL_VIDEO_DATA = "
        + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    return output_dir / "index.html"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render selectable UI concept prototypes")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(render(args.manifest, args.output_dir))


if __name__ == "__main__":
    main()
