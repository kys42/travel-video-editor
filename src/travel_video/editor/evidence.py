from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from ..phase1 import _font, format_time
from .catalog import TimelineCatalog

EVIDENCE_SCHEMA = "scene-range-evidence/v1"
EVIDENCE_CACHE_SCHEMA = "scene-contact-sheet-cache/v1"
_ARTIFACT_RE = re.compile(r"^evidence_([0-9a-f]{24})$")


@dataclass(frozen=True, slots=True)
class EvidenceInspection:
    card: dict[str, Any]
    model: dict[str, Any]
    local_image_paths: tuple[str, ...]


def _overlaps(start: float, end: float, other_start: float, other_end: float) -> bool:
    return start < other_end and end > other_start


def _bounded_text(value: Any, limit: int = 1200) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class SceneEvidenceService:
    """Build bounded, cached visual and transcript evidence for one scene range."""

    def __init__(
        self,
        catalog: TimelineCatalog,
        cache_root: Path,
        *,
        max_range_seconds: float = 180.0,
        max_frames: int = 12,
        max_context_seconds: float = 15.0,
        max_text_characters: int = 12000,
        max_raw_candidates: int = 80,
    ) -> None:
        self.catalog = catalog
        self.cache_root = cache_root.expanduser().resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.max_range_seconds = max_range_seconds
        self.max_frames = max_frames
        self.max_context_seconds = max_context_seconds
        self.max_text_characters = max_text_characters
        self.max_raw_candidates = max_raw_candidates

    def inspect(
        self,
        *,
        scene_id: str,
        source_in: float | None = None,
        source_out: float | None = None,
        visual_mode: str = "auto",
        frame_count: int = 8,
        include_raw_stt: bool = False,
        context_seconds: float = 4.0,
    ) -> EvidenceInspection:
        scene = self.catalog.scene(scene_id)
        start = scene.source_in if source_in is None else float(source_in)
        end = scene.source_out if source_out is None else float(source_out)
        if start < scene.source_in - 0.001 or end > scene.source_out + 0.001:
            raise ValueError("inspection range must stay inside the reviewed scene")
        if end <= start:
            raise ValueError("source_out must be greater than source_in")
        if end - start > self.max_range_seconds:
            raise ValueError(
                f"inspection range cannot exceed {self.max_range_seconds:g} seconds"
            )
        if visual_mode not in {"none", "auto", "existing", "proxy"}:
            raise ValueError("visual_mode must be none, auto, existing, or proxy")
        if not 1 <= frame_count <= self.max_frames:
            raise ValueError(f"frame_count must be between 1 and {self.max_frames}")
        if not 0 <= context_seconds <= self.max_context_seconds:
            raise ValueError(
                f"context_seconds must be between 0 and {self.max_context_seconds:g}"
            )

        transcript = self._transcript_evidence(
            scene_id=scene_id,
            start=start,
            end=end,
            context_seconds=context_seconds,
            include_raw_stt=include_raw_stt,
        )
        visual, image_paths = self._visual_evidence(
            scene_id=scene_id,
            start=start,
            end=end,
            visual_mode=visual_mode,
            frame_count=frame_count,
        )
        evidence_id = visual.get("artifact_id") or self._text_evidence_id(
            scene_id, start, end, include_raw_stt, context_seconds
        )
        base = {
            "schema_version": EVIDENCE_SCHEMA,
            "evidence_id": evidence_id,
            "scene_id": scene.scene_id,
            "asset_id": scene.asset_id,
            "title": scene.title,
            "source_in": round(start, 3),
            "source_out": round(end, 3),
            "duration": round(end - start, 3),
            "summary": scene.summary,
            "visual": visual,
            "transcript": transcript,
        }
        card = {"type": "scene_deep_evidence", **base}
        model = {
            **base,
            "visual": {
                key: value
                for key, value in visual.items()
                if key not in {"contact_sheet_url"}
            },
            "grounding_notice": (
                "Reviewed captions are authoritative display dialogue. Raw STT candidates "
                "are unverified secondary evidence. The contact sheet is attached as image "
                "input only when visual.status is ready."
            ),
        }
        return EvidenceInspection(card, model, tuple(image_paths))

    def contact_sheet_path(self, artifact_id: str) -> Path:
        matched = _ARTIFACT_RE.fullmatch(artifact_id)
        if not matched:
            raise KeyError(f"evidence artifact not found: {artifact_id}")
        path = (self.cache_root / matched.group(1) / "contact-sheet.jpg").resolve()
        if not path.is_relative_to(self.cache_root) or not path.is_file():
            raise KeyError(f"evidence artifact not found: {artifact_id}")
        return path

    @staticmethod
    def _text_evidence_id(
        scene_id: str,
        start: float,
        end: float,
        include_raw_stt: bool,
        context_seconds: float,
    ) -> str:
        raw = json.dumps(
            [
                scene_id,
                round(start, 3),
                round(end, 3),
                include_raw_stt,
                context_seconds,
            ],
            separators=(",", ":"),
        ).encode()
        return f"evidence_{hashlib.sha256(raw).hexdigest()[:24]}"

    def _transcript_evidence(
        self,
        *,
        scene_id: str,
        start: float,
        end: float,
        context_seconds: float,
        include_raw_stt: bool,
    ) -> dict[str, Any]:
        scene = self.catalog.scene(scene_id)
        timeline = scene.timeline
        context_start = max(0.0, start - context_seconds)
        context_end = min(float(timeline["media"]["duration"]), end + context_seconds)
        reviewed: list[dict[str, Any]] = []
        utterances: list[dict[str, Any]] = []
        raw_candidates: list[dict[str, Any]] = []
        raw_languages: set[str] = set()

        for group in timeline.get("context_groups", []):
            group_start = float(group.get("start", 0.0))
            group_end = float(group.get("end", group_start))
            if not _overlaps(context_start, context_end, group_start, group_end):
                continue
            reviewed_dialogue = group.get("reviewed_dialogue")
            if isinstance(reviewed_dialogue, dict):
                for caption in reviewed_dialogue.get("captions") or []:
                    if not isinstance(caption, dict):
                        continue
                    item_start = float(
                        caption.get("source_start", caption.get("start", group_start))
                    )
                    item_end = float(
                        caption.get("source_end", caption.get("end", item_start))
                    )
                    if not _overlaps(context_start, context_end, item_start, item_end):
                        continue
                    text = _bounded_text(caption.get("display_text"))
                    if text:
                        reviewed.append(
                            {
                                "caption_id": caption.get("caption_id"),
                                "source_in": round(item_start, 3),
                                "source_out": round(item_end, 3),
                                "language": caption.get("language"),
                                "text": text,
                                "relation": (
                                    "selected"
                                    if _overlaps(start, end, item_start, item_end)
                                    else "context"
                                ),
                                "review_status": caption.get("review_status"),
                                "provenance": "reviewed_caption",
                            }
                        )
                source_rows = reviewed_dialogue.get("utterances") or []
                source_kind = "reviewed_utterance"
            else:
                source_rows = group.get("reconciled_utterances") or []
                source_kind = "reconciled_utterance"
            for utterance in source_rows:
                if not isinstance(utterance, dict):
                    continue
                item_start = float(
                    utterance.get("source_start", utterance.get("start", group_start))
                )
                item_end = float(
                    utterance.get("source_end", utterance.get("end", item_start))
                )
                if not _overlaps(context_start, context_end, item_start, item_end):
                    continue
                text = _bounded_text(
                    utterance.get("original_text", utterance.get("text", ""))
                )
                if text:
                    utterances.append(
                        {
                            "utterance_id": utterance.get("utterance_id"),
                            "source_in": round(item_start, 3),
                            "source_out": round(item_end, 3),
                            "language": utterance.get("language"),
                            "text": text,
                            "relation": (
                                "selected"
                                if _overlaps(start, end, item_start, item_end)
                                else "context"
                            ),
                            "confidence": _finite_number(utterance.get("confidence")),
                            "provenance": source_kind,
                        }
                    )

        for segment in timeline.get("segments", []):
            segment_start = float(segment.get("start", 0.0))
            segment_end = float(segment.get("end", segment_start))
            if not _overlaps(context_start, context_end, segment_start, segment_end):
                continue
            for language, candidates in (
                segment.get("transcript_candidates") or {}
            ).items():
                raw_languages.add(str(language))
                if not include_raw_stt:
                    continue
                for candidate in candidates or []:
                    if not isinstance(candidate, dict):
                        continue
                    item_start = float(candidate.get("start", segment_start))
                    item_end = float(candidate.get("end", segment_end))
                    if not _overlaps(context_start, context_end, item_start, item_end):
                        continue
                    text = _bounded_text(candidate.get("text"))
                    if text:
                        raw_candidates.append(
                            {
                                "segment_id": segment.get("segment_id"),
                                "source_in": round(item_start, 3),
                                "source_out": round(item_end, 3),
                                "language": str(language),
                                "text": text,
                                "relation": (
                                    "selected"
                                    if _overlaps(start, end, item_start, item_end)
                                    else "context"
                                ),
                                "avg_logprob": _finite_number(
                                    candidate.get("avg_logprob")
                                ),
                                "no_speech_prob": _finite_number(
                                    candidate.get("no_speech_prob")
                                ),
                                "provenance": "raw_asr_candidate_unverified",
                            }
                        )

        reviewed.sort(key=lambda item: (item["source_in"], item["source_out"]))
        utterances.sort(key=lambda item: (item["source_in"], item["source_out"]))
        raw_candidates.sort(
            key=lambda item: (item["source_in"], item["source_out"], item["language"])
        )
        raw_truncated = len(raw_candidates) > self.max_raw_candidates
        raw_candidates = raw_candidates[: self.max_raw_candidates]
        reviewed, utterances, raw_candidates, text_truncated = (
            self._cap_transcript_text(reviewed, utterances, raw_candidates)
        )
        return {
            "context_seconds": round(context_seconds, 3),
            "reviewed_captions": reviewed,
            "source_utterances": utterances,
            "raw_stt_included": include_raw_stt,
            "raw_stt_languages": sorted(raw_languages),
            "raw_stt_candidates": raw_candidates,
            "truncated": raw_truncated or text_truncated,
        }

    def _cap_transcript_text(
        self,
        reviewed: list[dict[str, Any]],
        utterances: list[dict[str, Any]],
        raw_candidates: list[dict[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        bool,
    ]:
        remaining = self.max_text_characters
        output: list[list[dict[str, Any]]] = []
        truncated = False
        for rows in (reviewed, utterances, raw_candidates):
            accepted: list[dict[str, Any]] = []
            for row in rows:
                length = len(str(row.get("text", "")))
                if length > remaining:
                    truncated = True
                    break
                accepted.append(row)
                remaining -= length
            if len(accepted) < len(rows):
                truncated = True
            output.append(accepted)
        return output[0], output[1], output[2], truncated

    def _visual_evidence(
        self,
        *,
        scene_id: str,
        start: float,
        end: float,
        visual_mode: str,
        frame_count: int,
    ) -> tuple[dict[str, Any], list[str]]:
        if visual_mode == "none":
            return {"status": "not_requested", "source": None, "frame_count": 0}, []

        scene = self.catalog.scene(scene_id)
        asset = self.catalog.asset(scene.asset_id)
        existing = self._existing_frames(scene.timeline, start, end, frame_count)
        proxy = Path(asset.proxy_path) if asset.proxy_path else None
        proxy_ready = bool(proxy and proxy.is_file())
        source = visual_mode
        if visual_mode == "auto":
            source = (
                "proxy"
                if proxy_ready and len(existing) < min(4, frame_count)
                else "existing"
            )
        if source == "proxy" and not proxy_ready:
            if visual_mode == "proxy":
                raise ValueError("the selected asset has no readable proxy video")
            source = "existing"

        if source == "proxy":
            assert proxy is not None
            key_source = self._proxy_key(proxy, scene_id, start, end, frame_count)
            try:
                artifact_id, sheet, cells, cached = self._cached_sheet(
                    key_source,
                    lambda directory: self._extract_proxy_frames(
                        proxy, directory, start, end, frame_count
                    ),
                    source="proxy",
                )
            except RuntimeError as exc:
                if visual_mode != "auto":
                    raise ValueError("proxy contact-sheet extraction failed") from exc
                if not existing:
                    return {
                        "status": "unavailable",
                        "source": None,
                        "frame_count": 0,
                        "reason": "Proxy frame extraction failed; transcript evidence remains available.",
                    }, []
                source = "existing"
                key_source = self._existing_key(existing, scene_id, start, end)
                artifact_id, sheet, cells, cached = self._cached_sheet(
                    key_source,
                    lambda _: existing,
                    source="analysis_frames",
                )
        elif existing:
            key_source = self._existing_key(existing, scene_id, start, end)
            artifact_id, sheet, cells, cached = self._cached_sheet(
                key_source,
                lambda _: existing,
                source="analysis_frames",
            )
        else:
            return {
                "status": "unavailable",
                "source": None,
                "frame_count": 0,
                "reason": "No analyzed frames or readable proxy are available for this range.",
            }, []

        return {
            "status": "ready",
            "source": source if source == "proxy" else "analysis_frames",
            "artifact_id": artifact_id,
            "contact_sheet_url": f"/api/evidence/contact-sheets/{artifact_id}.jpg",
            "frame_count": len(cells),
            "cached": cached,
            "cells": cells,
            "attached_to_agent": True,
        }, [str(sheet)]

    @staticmethod
    def _existing_frames(
        timeline: dict[str, Any], start: float, end: float, frame_count: int
    ) -> list[dict[str, Any]]:
        candidates = [
            {
                "path": str(sample.get("frame")),
                "source_time": float(sample.get("time", 0.0)),
                "sample_id": sample.get("sample_id"),
            }
            for sample in timeline.get("samples", [])
            if start <= float(sample.get("time", -1.0)) <= end
            and sample.get("frame")
            and Path(str(sample["frame"])).is_file()
        ]
        if len(candidates) <= frame_count:
            return candidates
        if frame_count == 1:
            return [candidates[len(candidates) // 2]]
        indices = {
            round(index * (len(candidates) - 1) / (frame_count - 1))
            for index in range(frame_count)
        }
        return [candidates[index] for index in sorted(indices)]

    @staticmethod
    def _proxy_key(
        proxy: Path, scene_id: str, start: float, end: float, frame_count: int
    ) -> bytes:
        stat = proxy.stat()
        return json.dumps(
            [
                EVIDENCE_CACHE_SCHEMA,
                "proxy",
                scene_id,
                round(start, 3),
                round(end, 3),
                frame_count,
                stat.st_size,
                stat.st_mtime_ns,
            ],
            separators=(",", ":"),
        ).encode()

    @staticmethod
    def _existing_key(
        frames: list[dict[str, Any]], scene_id: str, start: float, end: float
    ) -> bytes:
        rows = []
        for frame in frames:
            path = Path(frame["path"])
            stat = path.stat()
            rows.append(
                [
                    str(path),
                    stat.st_size,
                    stat.st_mtime_ns,
                    round(float(frame["source_time"]), 6),
                    frame.get("sample_id"),
                ]
            )
        return json.dumps(
            [EVIDENCE_CACHE_SCHEMA, "existing", scene_id, start, end, rows],
            separators=(",", ":"),
        ).encode()

    def _cached_sheet(
        self,
        key_source: bytes,
        producer: Callable[[Path], list[dict[str, Any]]],
        *,
        source: str,
    ) -> tuple[str, Path, list[dict[str, Any]], bool]:
        digest = hashlib.sha256(key_source).hexdigest()[:24]
        artifact_id = f"evidence_{digest}"
        final_dir = self.cache_root / digest
        sheet = final_dir / "contact-sheet.jpg"
        manifest = final_dir / "manifest.json"
        cached_cells = self._cached_cells(sheet, manifest)
        if cached_cells is not None:
            return artifact_id, sheet, cached_cells, True

        # FastAPI may dispatch simultaneous sync evidence requests in separate
        # worker threads. A per-artifact file lock keeps partial cache recovery
        # and the final atomic rename deterministic.
        lock_path = self.cache_root / f".{digest}.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            cached_cells = self._cached_cells(sheet, manifest)
            if cached_cells is not None:
                return artifact_id, sheet, cached_cells, True

            if final_dir.exists():
                shutil.rmtree(final_dir)
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{digest}.partial-", dir=self.cache_root)
            )
            try:
                frames = producer(temporary)
                cells = self._create_contact_sheet(
                    frames, temporary / "contact-sheet.jpg"
                )
                (temporary / "manifest.json").write_text(
                    json.dumps(
                        {
                            "schema_version": EVIDENCE_CACHE_SCHEMA,
                            "artifact_id": artifact_id,
                            "source": source,
                            "cells": cells,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, final_dir)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return artifact_id, sheet, cells, False

    @staticmethod
    def _cached_cells(sheet: Path, manifest: Path) -> list[dict[str, Any]] | None:
        if not sheet.is_file() or not manifest.is_file():
            return None
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            cells = payload["cells"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None
        if payload.get("schema_version") != EVIDENCE_CACHE_SCHEMA or not isinstance(
            cells, list
        ):
            return None
        return [item for item in cells if isinstance(item, dict)]

    @staticmethod
    def _extract_proxy_frames(
        proxy: Path,
        directory: Path,
        start: float,
        end: float,
        frame_count: int,
    ) -> list[dict[str, Any]]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for proxy contact-sheet extraction")
        duration = end - start
        frame_dir = directory / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{start:.6f}",
                    "-t",
                    f"{duration:.6f}",
                    "-i",
                    str(proxy),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-sn",
                    "-vf",
                    f"fps={frame_count / duration:.9f},scale=480:-2:flags=lanczos",
                    "-frames:v",
                    str(frame_count),
                    "-q:v",
                    "4",
                    str(frame_dir / "frame_%03d.jpg"),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("proxy frame extraction timed out") from exc
        paths = sorted(frame_dir.glob("frame_*.jpg"))
        if completed.returncode or not paths:
            message = completed.stderr.strip()[-500:]
            raise RuntimeError(f"proxy frame extraction failed: {message}")
        return [
            {
                "path": str(path),
                "source_time": round(
                    min(end, start + (index + 0.5) * duration / len(paths)), 3
                ),
                "sample_id": None,
            }
            for index, path in enumerate(paths)
        ]

    @staticmethod
    def _create_contact_sheet(
        frames: list[dict[str, Any]], output_path: Path
    ) -> list[dict[str, Any]]:
        if not frames:
            raise RuntimeError("no frames are available for a contact sheet")
        columns = min(4, len(frames))
        tile_width, image_height, label_height = 320, 180, 34
        rows = math.ceil(len(frames) / columns)
        canvas = Image.new(
            "RGB",
            (columns * tile_width, rows * (image_height + label_height)),
            "#101319",
        )
        draw = ImageDraw.Draw(canvas)
        font = _font(15)
        cells: list[dict[str, Any]] = []
        for index, frame in enumerate(frames):
            row, column = divmod(index, columns)
            x, y = column * tile_width, row * (image_height + label_height)
            with Image.open(frame["path"]) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                fitted = ImageOps.pad(
                    image, (tile_width, image_height), color="#000000"
                )
            canvas.paste(fitted, (x, y))
            cell = f"{chr(65 + row)}{column + 1}"
            source_time = float(frame["source_time"])
            draw.rectangle(
                (x, y + image_height, x + tile_width, y + image_height + label_height),
                fill="#1a202a",
            )
            draw.text(
                (x + 8, y + image_height + 7),
                f"{cell}  {format_time(source_time)}",
                font=font,
                fill="#f4f7fb",
            )
            cells.append(
                {
                    "cell": cell,
                    "source_time": round(source_time, 3),
                    "timecode": format_time(source_time),
                    "sample_id": frame.get("sample_id"),
                }
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, quality=88, optimize=True)
        return cells
