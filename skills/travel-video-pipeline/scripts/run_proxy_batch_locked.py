#!/usr/bin/env python3
"""Run proxy-batch with a non-blocking lock per output root."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def active_unlocked_writers(output_root: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    matches: list[dict[str, Any]] = []
    needle = str(output_root)
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        pid = int(pid_text)
        if pid == os.getpid() or needle not in command:
            continue
        if "travel-video proxy-batch" in command or (
            "ffmpeg" in command and ".partial.mp4" in command
        ):
            matches.append({"pid": pid, "command": command})
    return matches


def write_state(handle: Any, state: dict[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(state, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--max-dimension", type=int, default=1920)
    parser.add_argument("--bitrate-kbps", type=int, default=6000)
    parser.add_argument("--high-fps-bitrate-kbps", type=int, default=8000)
    args = parser.parse_args()

    project_root = args.project_root.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    cli = project_root / ".venv/bin/travel-video"
    if not cli.is_file():
        raise ValueError("project CLI is missing; run uv sync")
    if not source_root.is_dir():
        raise ValueError("source root is not an existing directory")
    output_root.mkdir(parents=True, exist_ok=True)

    writers = active_unlocked_writers(output_root)
    if writers:
        raise ValueError(f"active proxy writer already targets output root: {writers}")
    lock_path = output_root / ".proxy-batch.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip()
            raise ValueError(f"proxy output lock is held: {owner}") from exc
        state = {
            "schema_version": "travel-video-proxy-lock/v1",
            "status": "running",
            "pid": os.getpid(),
            "host": platform.node(),
            "started_at": now(),
            "source_root": str(source_root),
            "output_root": str(output_root),
        }
        write_state(handle, state)
        command = [
            str(cli),
            "proxy-batch",
            str(source_root),
            "--output-root",
            str(output_root),
            "--max-dimension",
            str(args.max_dimension),
            "--bitrate-kbps",
            str(args.bitrate_kbps),
            "--high-fps-bitrate-kbps",
            str(args.high_fps_bitrate_kbps),
        ]
        result = subprocess.run(command, cwd=project_root, check=False)
        state.update(
            {
                "status": "complete" if result.returncode == 0 else "failed",
                "ended_at": now(),
                "returncode": result.returncode,
            }
        )
        write_state(handle, state)
        return result.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
