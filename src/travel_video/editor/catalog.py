from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..review import load_json
from ..video_summary import VIDEO_SUMMARIZED_TIMELINE_SCHEMA


def make_scene_id(asset_id: str, group_id: str) -> str:
    return f"{asset_id}:{group_id}"


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[\w가-힣]+", value, flags=re.UNICODE)
        if len(token) > 1
    }


def _short_text(value: str, limit: int = 220) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _reviewed_captions(group: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Return authoritative reviewed captions without reviving legacy dialogue."""
    reviewed = group.get("reviewed_dialogue")
    if not isinstance(reviewed, dict) or "captions" not in reviewed:
        return False, []
    captions = reviewed.get("captions")
    if not isinstance(captions, list):
        return True, []
    return True, [item for item in captions if isinstance(item, dict)]


@dataclass(frozen=True, slots=True)
class AssetRecord:
    asset_id: str
    source_name: str
    source_path: str
    proxy_path: str | None
    creation_time: str | None
    duration: float
    title: str
    summary: str
    tags: tuple[str, ...]
    scene_count: int
    timeline_path: str

    def compact(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "source_name": self.source_name,
            "creation_time": self.creation_time,
            "duration": round(self.duration, 3),
            "title": self.title,
            "summary": _short_text(self.summary, 180),
            "tags": list(self.tags),
            "scene_count": self.scene_count,
            "proxy_ready": bool(self.proxy_path),
        }


@dataclass(frozen=True, slots=True)
class SceneRecord:
    scene_id: str
    asset_id: str
    group_id: str
    source_in: float
    source_out: float
    title: str
    label: str
    summary: str
    dialogue_summary: str
    dialogue_excerpt: str
    notable_moments: tuple[dict[str, Any], ...]
    confidence: float
    highlighted: bool
    representative_frame: str | None
    search_text: str
    timeline: dict[str, Any]
    group: dict[str, Any]

    def compact(self, score: float | None = None) -> dict[str, Any]:
        value = {
            "scene_id": self.scene_id,
            "asset_id": self.asset_id,
            "group_id": self.group_id,
            "source_in": round(self.source_in, 3),
            "source_out": round(self.source_out, 3),
            "duration": round(self.source_out - self.source_in, 3),
            "title": self.title,
            "label": self.label,
            "summary": _short_text(self.summary),
            "dialogue_excerpt": _short_text(
                self.dialogue_excerpt or self.dialogue_summary, 140
            ),
            "notable_titles": [
                str(item.get("title", "")) for item in self.notable_moments
            ],
            "confidence": round(self.confidence, 3),
            "highlighted": self.highlighted,
            "dom_id": f"scene-{self.asset_id}-{self.group_id}",
        }
        if score is not None:
            value["score"] = round(score, 4)
        return value


class TimelineCatalog:
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.expanduser().resolve()
        manifest = load_json(self.manifest_path)
        timeline_paths = [Path(item) for item in manifest.get("timelines", [])]
        if not timeline_paths:
            raise ValueError("library manifest does not contain timelines")
        self.title = str(manifest.get("title") or "Travel Video Editor")
        self.library_dir = self.manifest_path.parent
        self.proxy_root = (
            Path(manifest["proxy_root"]).expanduser().resolve()
            if manifest.get("proxy_root")
            else None
        )
        proxies = self._discover_proxies(self.proxy_root)
        self._assets: dict[str, AssetRecord] = {}
        self._scenes: dict[str, SceneRecord] = {}
        for path in timeline_paths:
            self._load_timeline(path.expanduser().resolve(), proxies)
        self._ordered_assets = sorted(
            self._assets.values(),
            key=lambda item: (item.creation_time or "9999", item.source_name),
        )
        self._ordered_scenes = sorted(
            self._scenes.values(),
            key=lambda item: (
                self._assets[item.asset_id].creation_time or "9999",
                item.source_in,
            ),
        )

    @staticmethod
    def _discover_proxies(proxy_root: Path | None) -> dict[str, str]:
        if proxy_root is None or not proxy_root.is_dir():
            return {}
        matches: dict[str, list[Path]] = {}
        for path in proxy_root.rglob("*.mp4"):
            if path.is_file() and not path.stem.casefold().endswith(".partial"):
                matches.setdefault(path.name.casefold(), []).append(path.resolve())
        return {
            name: str(paths[0]) for name, paths in matches.items() if len(paths) == 1
        }

    def _load_timeline(self, path: Path, proxies: dict[str, str]) -> None:
        timeline = load_json(path)
        if timeline.get("schema_version") != VIDEO_SUMMARIZED_TIMELINE_SCHEMA:
            raise ValueError(f"unsupported summarized timeline: {path}")
        asset_id = str(timeline["asset_id"])
        source_name = str(timeline["source"]["name"])
        summary = timeline["video_summary"]
        self._assets[asset_id] = AssetRecord(
            asset_id=asset_id,
            source_name=source_name,
            source_path=str(timeline["source"]["path"]),
            proxy_path=proxies.get(source_name.casefold()),
            creation_time=timeline["media"].get("creation_time"),
            duration=float(timeline["media"]["duration"]),
            title=str(summary["title"]),
            summary=str(summary["one_line_summary"]),
            tags=tuple(str(item) for item in summary.get("tags", [])),
            scene_count=len(timeline.get("context_groups", [])),
            timeline_path=str(path),
        )
        events = {
            str(item["group_id"]): item
            for item in summary.get("chronological_events", [])
        }
        samples = {str(item["sample_id"]): item for item in timeline.get("samples", [])}
        highlight_ids = set(summary.get("highlight_group_ids", []))
        segment_map = {
            str(item["segment_id"]): item for item in timeline.get("segments", [])
        }
        for group in timeline.get("context_groups", []):
            group_id = str(group["group_id"])
            context = group.get("context_review") or {}
            event = events.get(group_id, {})
            has_reviewed_captions, captions = _reviewed_captions(group)
            if has_reviewed_captions:
                dialogue_excerpt = " ".join(
                    str(item.get("display_text", "")).strip()
                    for item in captions
                    if str(item.get("display_text", "")).strip()
                )
                dialogue_summary = dialogue_excerpt
            else:
                utterances = group.get("reconciled_utterances") or []
                dialogue_excerpt = " ".join(
                    str(item.get("original_text", "")).strip()
                    for item in utterances
                    if str(item.get("original_text", "")).strip()
                )
                dialogue_summary = str(context.get("dialogue_summary") or "")
            search_parts = [
                str(event.get("headline", "")),
                str(event.get("description", "")),
                str(group.get("label", "")),
                str(context.get("narrative_summary", "")),
                dialogue_summary,
                dialogue_excerpt,
                *self._assets[asset_id].tags,
            ]
            notables = tuple(context.get("notable_moments", []))
            for notable in notables:
                search_parts.extend(
                    str(notable.get(key, ""))
                    for key in ("title", "description", "edit_hint", "category")
                )
            for segment_id in group.get("segment_ids", []):
                segment = segment_map.get(str(segment_id), {})
                review = segment.get("review") or {}
                search_parts.append(str(review.get("visual_summary", "")))
                search_parts.extend(str(item) for item in review.get("actions", []))
                if not has_reviewed_captions:
                    for values in segment.get("transcript_candidates", {}).values():
                        search_parts.extend(
                            str(item.get("text", "")) for item in values
                        )
            representative = samples.get(
                str(context.get("representative_sample_id", "")), {}
            )
            scene = SceneRecord(
                scene_id=make_scene_id(asset_id, group_id),
                asset_id=asset_id,
                group_id=group_id,
                source_in=float(group["start"]),
                source_out=float(group["end"]),
                title=str(event.get("headline") or group.get("label") or group_id),
                label=str(group.get("label") or event.get("headline") or group_id),
                summary=str(
                    context.get("narrative_summary") or event.get("description") or ""
                ),
                dialogue_summary=dialogue_summary,
                dialogue_excerpt=dialogue_excerpt,
                notable_moments=notables,
                confidence=float(context.get("confidence", 0.0)),
                highlighted=group_id in highlight_ids,
                representative_frame=(
                    str(representative["frame"])
                    if representative.get("frame")
                    else None
                ),
                search_text=" ".join(search_parts).casefold(),
                timeline=timeline,
                group=group,
            )
            self._scenes[scene.scene_id] = scene

    @property
    def asset_count(self) -> int:
        return len(self._assets)

    @property
    def scene_count(self) -> int:
        return len(self._scenes)

    def asset(self, asset_id: str) -> AssetRecord:
        try:
            return self._assets[asset_id]
        except KeyError as exc:
            raise KeyError(f"asset not found: {asset_id}") from exc

    def scene(self, scene_id: str) -> SceneRecord:
        try:
            return self._scenes[scene_id]
        except KeyError as exc:
            raise KeyError(f"scene not found: {scene_id}") from exc

    def list_assets(
        self, *, capture_date: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        rows = self._ordered_assets
        if capture_date:
            try:
                datetime.strptime(capture_date, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("capture_date must be YYYY-MM-DD") from exc
            rows = [
                item
                for item in rows
                if (item.creation_time or "").startswith(capture_date)
            ]
        return [item.compact() for item in rows[:limit]]

    def resolve_ui_context(
        self, state: dict[str, Any], *, limits: dict[str, Any]
    ) -> dict[str, Any]:
        """Turn lightweight browser state into an authoritative compact snapshot."""
        if not isinstance(state, dict):
            raise ValueError("editor UI context must be an object")
        schema_version = state.get("schema_version")
        if schema_version not in {None, "editor-ui-state/v1"}:
            raise ValueError(f"unsupported editor UI state: {schema_version}")

        current_asset_id = str(
            state.get("current_asset_id") or state.get("active_asset_id") or ""
        ).strip()
        focused_scene_id = str(
            state.get("focused_scene_id") or state.get("active_scene_id") or ""
        ).strip()
        selected_raw = state.get("selected_scene_ids") or []
        visible_raw = state.get("visible_asset_ids") or []
        if not isinstance(selected_raw, list):
            raise ValueError("selected_scene_ids must be an array")
        if not isinstance(visible_raw, list):
            raise ValueError("visible_asset_ids must be an array")
        selected_limit = int(limits["selected_scene_count"])
        visible_limit = int(limits["visible_asset_id_count"])
        day_limit = int(limits["project_day_rollup_count"])
        asset_limit = int(limits["project_asset_index_count"])
        nearby_limit = int(limits["nearby_asset_count"])
        if len(selected_raw) > selected_limit:
            raise ValueError(
                f"selected_scene_ids cannot contain more than {selected_limit} items"
            )
        if len(visible_raw) > visible_limit:
            raise ValueError(
                f"visible_asset_ids cannot contain more than {visible_limit} items"
            )

        selected_ids = list(dict.fromkeys(str(item) for item in selected_raw))
        visible_ids = list(dict.fromkeys(str(item) for item in visible_raw))
        selected_id_set = set(selected_ids)
        for scene_id in selected_id_set:
            self.scene(scene_id)
        selected = [
            scene for scene in self._ordered_scenes if scene.scene_id in selected_id_set
        ]
        if focused_scene_id:
            focused = self.scene(focused_scene_id)
            current_asset_id = current_asset_id or focused.asset_id
        else:
            focused = None
        current_asset = self.asset(current_asset_id) if current_asset_id else None
        if focused and current_asset and focused.asset_id != current_asset.asset_id:
            raise ValueError("focused scene does not belong to the current asset")
        for asset_id in visible_ids:
            self.asset(asset_id)

        selected_range = state.get("selected_range")
        compact_range: dict[str, float] | None = None
        if isinstance(selected_range, dict):
            try:
                source_in = float(selected_range["source_in"])
                source_out = float(selected_range["source_out"])
            except (KeyError, TypeError, ValueError):
                pass
            else:
                if source_in >= 0 and source_out >= source_in:
                    compact_range = {
                        "source_in": round(source_in, 3),
                        "source_out": round(source_out, 3),
                    }
        if (
            compact_range
            and focused
            and (
                compact_range["source_in"] < focused.source_in
                or compact_range["source_out"] > focused.source_out
            )
        ):
            raise ValueError("selected range is outside the focused scene")

        def context_scene(scene: SceneRecord) -> dict[str, Any]:
            return {
                "scene_id": scene.scene_id,
                "asset_id": scene.asset_id,
                "source_in": round(scene.source_in, 3),
                "source_out": round(scene.source_out, 3),
                "title": scene.title,
                "summary": _short_text(scene.summary, 160),
                "dialogue_excerpt": _short_text(
                    scene.dialogue_excerpt or scene.dialogue_summary, 100
                ),
                "notable_titles": [
                    str(item.get("title", ""))
                    for item in scene.notable_moments[:3]
                    if item.get("title")
                ],
                "highlighted": scene.highlighted,
            }

        day_rows: dict[str, dict[str, Any]] = {}
        tag_counts: dict[str, int] = {}
        for asset in self._ordered_assets:
            capture_date = (
                asset.creation_time[:10]
                if asset.creation_time and len(asset.creation_time) >= 10
                else "unknown"
            )
            row = day_rows.setdefault(
                capture_date,
                {
                    "capture_date": capture_date,
                    "asset_count": 0,
                    "scene_count": 0,
                    "duration": 0.0,
                    "titles": [],
                    "tag_counts": {},
                },
            )
            row["asset_count"] += 1
            row["scene_count"] += asset.scene_count
            row["duration"] += asset.duration
            if len(row["titles"]) < 5:
                row["titles"].append(asset.title)
            for tag in asset.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
                row["tag_counts"][tag] = row["tag_counts"].get(tag, 0) + 1

        rollups = []
        for row in day_rows.values():
            rollups.append(
                {
                    "capture_date": row["capture_date"],
                    "asset_count": row["asset_count"],
                    "scene_count": row["scene_count"],
                    "duration": round(row["duration"], 3),
                    "titles": row["titles"],
                    "top_tags": [
                        tag
                        for tag, _ in sorted(
                            row["tag_counts"].items(),
                            key=lambda item: (-item[1], item[0]),
                        )[:6]
                    ],
                }
            )
        known_dates = [key for key in day_rows if key != "unknown"]
        asset_index = [
            {
                "asset_id": asset.asset_id,
                "creation_time": asset.creation_time,
                "duration": round(asset.duration, 3),
                "title": asset.title,
                "tags": list(asset.tags[:5]),
            }
            for asset in self._ordered_assets[:asset_limit]
        ]
        nearby_assets: list[dict[str, Any]] = []
        if current_asset:
            current_index = self._ordered_assets.index(current_asset)
            before = nearby_limit // 2
            window_start = max(0, current_index - before)
            window_end = min(len(self._ordered_assets), window_start + nearby_limit)
            window_start = max(0, window_end - nearby_limit)
            nearby_assets = [
                asset.compact()
                for asset in self._ordered_assets[window_start:window_end]
            ]

        return {
            "schema_version": "editor-ui-context/v1",
            "project": {
                "title": self.title,
                "asset_count": self.asset_count,
                "scene_count": self.scene_count,
                "total_duration": round(
                    sum(asset.duration for asset in self._ordered_assets), 3
                ),
                "capture_date_range": {
                    "start": min(known_dates) if known_dates else None,
                    "end": max(known_dates) if known_dates else None,
                },
                "capture_day_count": len(day_rows),
                "top_tags": [
                    tag
                    for tag, _ in sorted(
                        tag_counts.items(), key=lambda item: (-item[1], item[0])
                    )[:10]
                ],
                "day_rollups": rollups[:day_limit],
                "day_rollups_truncated": len(rollups) > day_limit,
                "asset_index": asset_index,
                "asset_index_truncated": self.asset_count > len(asset_index),
                "timeline_order": "capture_time_asc",
            },
            "workspace": {
                "current_asset": current_asset.compact() if current_asset else None,
                "nearby_assets": nearby_assets,
                "focused_scene": context_scene(focused) if focused else None,
                "selected_scenes": [context_scene(scene) for scene in selected],
                "selected_scene_count": len(selected),
                "selected_asset_count": len({scene.asset_id for scene in selected}),
                "selection_is_explicit": bool(selected),
                "selection_order": "timeline",
                "visible_asset_ids": visible_ids,
                "search_query": _short_text(str(state.get("search_query") or ""), 160),
                "selected_range": compact_range,
                "inspector_tab": str(state.get("inspector_tab") or "ai")[:16],
            },
        }

    def search_scenes(
        self,
        *,
        query: str = "",
        asset_ids: list[str] | None = None,
        highlight_only: bool = False,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 30:
            raise ValueError("limit must be between 1 and 30")
        allowed = set(asset_ids or [])
        unknown = allowed - self._assets.keys()
        if unknown:
            raise KeyError(f"unknown asset IDs: {', '.join(sorted(unknown))}")
        query_tokens = _tokens(query)
        query_folded = query.casefold().strip()
        ranked: list[tuple[float, int, SceneRecord]] = []
        for order, scene in enumerate(self._ordered_scenes):
            if allowed and scene.asset_id not in allowed:
                continue
            if highlight_only and not scene.highlighted:
                continue
            scene_tokens = _tokens(scene.search_text)
            overlap = len(query_tokens & scene_tokens)
            substring = bool(query_folded and query_folded in scene.search_text)
            if query_tokens and overlap == 0 and not substring:
                continue
            score = overlap * 2.0 + (2.5 if substring else 0.0)
            score += 0.8 if scene.highlighted else 0.0
            score += min(1.0, len(scene.notable_moments) * 0.4)
            score += scene.confidence * 0.25
            ranked.append((score, -order, scene))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [scene.compact(score) for score, _, scene in ranked[:limit]]

    def scene_evidence(self, scene_id: str) -> dict[str, Any]:
        scene = self.scene(scene_id)
        group = scene.group
        timeline = scene.timeline
        has_reviewed_captions, reviewed_captions = _reviewed_captions(group)
        segments = {
            str(item["segment_id"]): item for item in timeline.get("segments", [])
        }
        samples = {str(item["sample_id"]): item for item in timeline.get("samples", [])}
        segment_rows = []
        for segment_id in group.get("segment_ids", []):
            item = segments.get(str(segment_id), {})
            review = item.get("review") or {}
            transcript_candidates: dict[str, list[dict[str, Any]]] = {}
            for language, candidates in item.get("transcript_candidates", {}).items():
                transcript_candidates[str(language)] = [
                    {
                        key: candidate[key]
                        for key in (
                            "text",
                            "start",
                            "end",
                            "avg_logprob",
                            "no_speech_prob",
                        )
                        if key in candidate
                    }
                    for candidate in candidates[:8]
                    if isinstance(candidate, dict)
                ]
            segment_rows.append(
                {
                    "segment_id": segment_id,
                    "source_in": item.get("start"),
                    "source_out": item.get("end"),
                    "visual_summary": review.get("visual_summary", ""),
                    "actions": review.get("actions", []),
                    "transcript_candidates": transcript_candidates,
                }
            )
        context = group.get("context_review") or {}
        frames = []
        for moment in context.get("key_moments", []):
            sample = samples.get(str(moment.get("sample_id")), {})
            frames.append(
                {
                    "sample_id": moment.get("sample_id"),
                    "time": sample.get("time"),
                    "timecode": sample.get("timecode"),
                    "role": moment.get("role"),
                    "frame_path": sample.get("frame"),
                }
            )
        return {
            **scene.compact(),
            "dialogue_summary": scene.dialogue_summary,
            "captions": [
                {
                    key: value
                    for key, value in {
                        "caption_id": caption.get("caption_id"),
                        "source_start": caption.get(
                            "source_start", caption.get("start")
                        ),
                        "source_end": caption.get("source_end", caption.get("end")),
                        "language": caption.get("language"),
                        "display_text": caption.get("display_text"),
                        "edit_type": caption.get("edit_type"),
                        "confidence": caption.get("confidence"),
                        "review_status": caption.get("review_status"),
                    }.items()
                    if value is not None
                }
                for caption in reviewed_captions[:30]
                if str(caption.get("display_text", "")).strip()
            ],
            "utterances": [
                {
                    key: utterance[key]
                    for key in (
                        "utterance_id",
                        "source_start",
                        "source_end",
                        "language",
                        "original_text",
                        "translations",
                        "confidence",
                    )
                    if key in utterance
                }
                for utterance in (
                    []
                    if has_reviewed_captions
                    else (group.get("reconciled_utterances") or [])[:30]
                )
                if isinstance(utterance, dict)
            ],
            "notable_moments": list(scene.notable_moments),
            "segments": segment_rows,
            "key_frames": frames,
            "representative_reason": context.get("representative_reason", ""),
        }
