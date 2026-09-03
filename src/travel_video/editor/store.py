from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .catalog import TimelineCatalog

EDIT_REVISION_SCHEMA = "video-edit-revision/v1"
EDIT_PLAN_SCHEMA = "video-edit-plan/v1"


class RevisionConflictError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class EditStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS edits (
                    edit_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    brief TEXT NOT NULL,
                    target_duration REAL,
                    head_revision_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edit_revisions (
                    revision_id TEXT PRIMARY KEY,
                    edit_id TEXT NOT NULL REFERENCES edits(edit_id),
                    parent_revision_id TEXT,
                    sequence INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    UNIQUE(edit_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    codex_thread_id TEXT,
                    active_edit_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    tool_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    input_json TEXT NOT NULL,
                    result_summary_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def ping(self) -> None:
        with self._connection() as connection:
            connection.execute("SELECT 1").fetchone()

    def create_edit(
        self,
        *,
        title: str,
        brief: str,
        target_duration: float | None = None,
        created_by: str = "user",
    ) -> dict[str, Any]:
        title = title.strip()
        brief = brief.strip()
        if not title or len(title) > 120:
            raise ValueError("title must contain 1 to 120 characters")
        if not brief or len(brief) > 2000:
            raise ValueError("brief must contain 1 to 2000 characters")
        if target_duration is not None and not 5 <= target_duration <= 7200:
            raise ValueError("target_duration must be between 5 and 7200 seconds")
        edit_id = _id("edit")
        revision_id = _id("rev")
        created_at = _now()
        snapshot = {
            "schema_version": EDIT_REVISION_SCHEMA,
            "edit_id": edit_id,
            "revision_id": revision_id,
            "parent_revision_id": None,
            "sequence": 1,
            "created_at": created_at,
            "created_by": created_by,
            "brief": brief,
            "target_duration": target_duration,
            "plan": {
                "schema_version": EDIT_PLAN_SCHEMA,
                "title": title,
                "output": {
                    "width": 1920,
                    "height": 1080,
                    "fps": "30000/1001",
                    "video_bitrate": "8M",
                    "audio_bitrate": "192k",
                    "loudness_lufs": -16,
                },
                "clips": [],
                "captions": [],
                "overlays": [],
                "provenance": {"service": "travel-video-editor"},
            },
            "change_summary": [{"type": "edit_created", "title": title}],
            "validation": {"status": "draft", "warnings": ["No clips yet"]},
        }
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO edits VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    edit_id,
                    title,
                    brief,
                    target_duration,
                    revision_id,
                    created_at,
                    created_at,
                ),
            )
            connection.execute(
                "INSERT INTO edit_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    edit_id,
                    None,
                    1,
                    json.dumps(snapshot, ensure_ascii=False),
                    created_at,
                    created_by,
                ),
            )
            connection.commit()
        return self.get_edit(edit_id)

    def get_edit(
        self, edit_id: str, revision_id: str | None = None
    ) -> dict[str, Any]:
        with self._connection() as connection:
            edit = connection.execute(
                "SELECT * FROM edits WHERE edit_id = ?", (edit_id,)
            ).fetchone()
            if edit is None:
                raise KeyError(f"edit not found: {edit_id}")
            selected = revision_id or edit["head_revision_id"]
            row = connection.execute(
                "SELECT * FROM edit_revisions WHERE edit_id = ? AND revision_id = ?",
                (edit_id, selected),
            ).fetchone()
            if row is None:
                raise KeyError(f"revision not found: {selected}")
            history = connection.execute(
                "SELECT revision_id, parent_revision_id, sequence, created_at, created_by "
                "FROM edit_revisions WHERE edit_id = ? ORDER BY sequence DESC LIMIT 30",
                (edit_id,),
            ).fetchall()
        snapshot = json.loads(row["snapshot_json"])
        return {
            "edit_id": edit_id,
            "title": edit["title"],
            "brief": edit["brief"],
            "target_duration": edit["target_duration"],
            "head_revision_id": edit["head_revision_id"],
            "revision": self._decorate_revision(snapshot),
            "history": [dict(item) for item in history],
        }

    @staticmethod
    def _decorate_revision(snapshot: dict[str, Any]) -> dict[str, Any]:
        clips = snapshot["plan"].get("clips", [])
        result = copy.deepcopy(snapshot)
        result["clip_count"] = len(clips)
        result["timeline_duration"] = round(
            sum(
                (float(item["source_out"]) - float(item["source_in"]))
                / float(item.get("speed", 1.0))
                for item in clips
            ),
            3,
        )
        return result

    def apply_operations(
        self,
        *,
        catalog: TimelineCatalog,
        edit_id: str,
        expected_revision_id: str,
        summary: str,
        operations: list[dict[str, Any]],
        created_by: str,
    ) -> dict[str, Any]:
        if not summary.strip() or len(summary) > 240:
            raise ValueError("summary must contain 1 to 240 characters")
        if not operations or len(operations) > 100:
            raise ValueError("operations must contain 1 to 100 items")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            edit = connection.execute(
                "SELECT * FROM edits WHERE edit_id = ?", (edit_id,)
            ).fetchone()
            if edit is None:
                raise KeyError(f"edit not found: {edit_id}")
            if edit["head_revision_id"] != expected_revision_id:
                raise RevisionConflictError(
                    f"expected {expected_revision_id}, current head is {edit['head_revision_id']}"
                )
            previous = connection.execute(
                "SELECT * FROM edit_revisions WHERE revision_id = ?",
                (expected_revision_id,),
            ).fetchone()
            snapshot = json.loads(previous["snapshot_json"])
            change_summary = self._mutate_snapshot(snapshot, operations, catalog)
            sequence = int(previous["sequence"]) + 1
            revision_id = _id("rev")
            created_at = _now()
            snapshot.update(
                {
                    "revision_id": revision_id,
                    "parent_revision_id": expected_revision_id,
                    "sequence": sequence,
                    "created_at": created_at,
                    "created_by": created_by,
                    "change_summary": [
                        {"type": "batch", "summary": summary.strip()},
                        *change_summary,
                    ],
                    "validation": self._validation(snapshot),
                }
            )
            plan = snapshot["plan"]
            connection.execute(
                "INSERT INTO edit_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    edit_id,
                    expected_revision_id,
                    sequence,
                    json.dumps(snapshot, ensure_ascii=False),
                    created_at,
                    created_by,
                ),
            )
            connection.execute(
                "UPDATE edits SET title = ?, target_duration = ?, head_revision_id = ?, updated_at = ? WHERE edit_id = ?",
                (
                    plan["title"],
                    snapshot.get("target_duration"),
                    revision_id,
                    created_at,
                    edit_id,
                ),
            )
            connection.commit()
        return self.get_edit(edit_id)

    def _mutate_snapshot(
        self,
        snapshot: dict[str, Any],
        operations: list[dict[str, Any]],
        catalog: TimelineCatalog,
    ) -> list[dict[str, Any]]:
        clips = snapshot["plan"]["clips"]
        changes: list[dict[str, Any]] = []
        supported = {
            "add_scene",
            "trim_clip",
            "move_clip",
            "remove_clip",
            "set_title",
            "set_target_duration",
        }
        for operation in operations:
            op = operation.get("op")
            if op not in supported:
                raise ValueError(f"unsupported edit operation: {op}")
            if op == "add_scene":
                scene = catalog.scene(str(operation.get("scene_id") or ""))
                asset = catalog.asset(scene.asset_id)
                source_in = float(operation.get("source_in", scene.source_in))
                source_out = float(operation.get("source_out", scene.source_out))
                if source_in < scene.source_in - 0.001 or source_out > scene.source_out + 0.001:
                    raise ValueError("add_scene range must stay inside the reviewed scene")
                if source_out <= source_in:
                    raise ValueError("source_out must be greater than source_in")
                clip = {
                    "id": _id("clip"),
                    "source": asset.source_path,
                    "source_in": round(source_in, 6),
                    "source_out": round(source_out, 6),
                    "label": str(operation.get("label") or scene.title)[:160],
                    "reason": str(operation.get("reason") or "Reviewed scene selection")[:600],
                    "speed": 1.0,
                    "volume_db": 0.0,
                    "metadata": {
                        "asset_id": scene.asset_id,
                        "scene_id": scene.scene_id,
                        "group_id": scene.group_id,
                        "analysis_confidence": scene.confidence,
                    },
                }
                if asset.proxy_path:
                    clip["proxy"] = asset.proxy_path
                clips.append(clip)
                changes.append({"type": "clip_added", "clip_id": clip["id"], "scene_id": scene.scene_id})
            elif op == "trim_clip":
                clip = self._find_clip(clips, operation.get("clip_id"))
                scene = catalog.scene(clip["metadata"]["scene_id"])
                source_in = float(operation.get("source_in", clip["source_in"]))
                source_out = float(operation.get("source_out", clip["source_out"]))
                if source_in < scene.source_in - 0.001 or source_out > scene.source_out + 0.001 or source_out <= source_in:
                    raise ValueError("trim range must be positive and stay inside the reviewed scene")
                clip["source_in"] = round(source_in, 6)
                clip["source_out"] = round(source_out, 6)
                changes.append({"type": "clip_trimmed", "clip_id": clip["id"]})
            elif op == "move_clip":
                clip = self._find_clip(clips, operation.get("clip_id"))
                destination = int(operation.get("to_index", -1))
                if destination < 0 or destination >= len(clips):
                    raise ValueError("to_index must point inside the current clip list")
                clips.remove(clip)
                clips.insert(destination, clip)
                changes.append({"type": "clip_moved", "clip_id": clip["id"], "to_index": destination})
            elif op == "remove_clip":
                clip = self._find_clip(clips, operation.get("clip_id"))
                clips.remove(clip)
                changes.append({"type": "clip_removed", "clip_id": clip["id"]})
            elif op == "set_title":
                title = str(operation.get("title") or "").strip()
                if not title or len(title) > 120:
                    raise ValueError("title must contain 1 to 120 characters")
                snapshot["plan"]["title"] = title
                changes.append({"type": "title_changed", "title": title})
            elif op == "set_target_duration":
                duration = float(operation.get("target_duration", 0))
                if not 5 <= duration <= 7200:
                    raise ValueError("target_duration must be between 5 and 7200 seconds")
                snapshot["target_duration"] = duration
                changes.append({"type": "target_duration_changed", "target_duration": duration})
        return changes

    @staticmethod
    def _find_clip(clips: list[dict[str, Any]], clip_id: Any) -> dict[str, Any]:
        for clip in clips:
            if clip["id"] == clip_id:
                return clip
        raise KeyError(f"clip not found: {clip_id}")

    @staticmethod
    def _validation(snapshot: dict[str, Any]) -> dict[str, Any]:
        clips = snapshot["plan"].get("clips", [])
        warnings = [] if clips else ["No clips yet"]
        return {"status": "valid" if clips else "draft", "warnings": warnings}

    def session(self, session_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM chat_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                now = _now()
                connection.execute(
                    "INSERT INTO chat_sessions VALUES (?, NULL, NULL, ?, ?)",
                    (session_id, now, now),
                )
                connection.commit()
                return {"session_id": session_id, "codex_thread_id": None, "active_edit_id": None}
            return dict(row)

    def update_session(
        self,
        session_id: str,
        *,
        codex_thread_id: str | None = None,
        active_edit_id: str | None = None,
    ) -> None:
        current = self.session(session_id)
        with self._connection() as connection:
            connection.execute(
                "UPDATE chat_sessions SET codex_thread_id = ?, active_edit_id = ?, updated_at = ? WHERE session_id = ?",
                (
                    codex_thread_id if codex_thread_id is not None else current.get("codex_thread_id"),
                    active_edit_id if active_edit_id is not None else current.get("active_edit_id"),
                    _now(),
                    session_id,
                ),
            )
            connection.commit()

    def log_tool_run(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_name: str,
        category: str,
        status: str,
        duration_ms: int,
        arguments: dict[str, Any],
        result_summary: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO tool_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    session_id,
                    tool_name,
                    category,
                    status,
                    duration_ms,
                    json.dumps(arguments, ensure_ascii=False),
                    json.dumps(result_summary, ensure_ascii=False),
                    _now(),
                ),
            )
            connection.commit()
