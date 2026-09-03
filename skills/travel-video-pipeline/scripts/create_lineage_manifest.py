#!/usr/bin/env python3
"""Create a checked original-to-processing-input lineage sidecar."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def quick_fingerprint(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if size > 1024 * 1024:
            handle.seek(max(0, size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    value = json.loads(result.stdout)
    value["duration"] = float(value.get("format", {}).get("duration", 0.0))
    if value["duration"] <= 0:
        raise ValueError(f"no positive media duration: {path}")
    return value


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--processing-input", required=True, type=Path)
    parser.add_argument("--working-root", required=True, type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration-tolerance", type=float, default=0.25)
    args = parser.parse_args()

    source_root = args.source_root.expanduser().resolve()
    original = args.original.expanduser().resolve()
    processing = args.processing_input.expanduser().resolve()
    working_root = args.working_root.expanduser().resolve()
    project_root = args.project_root.expanduser().resolve() if args.project_root else None
    output = args.output.expanduser().resolve()
    if not original.is_file() or not processing.is_file():
        raise ValueError("original and processing input must both be files")
    if not is_within(original, source_root):
        raise ValueError("original is outside source root")
    if not is_within(processing, working_root):
        raise ValueError("processing input is outside working root")
    allowed_output = is_within(output, working_root) or (
        project_root is not None and is_within(output, project_root)
    )
    if not allowed_output:
        raise ValueError("lineage output must be inside working or project root")
    if is_within(output, source_root):
        raise ValueError("refusing to write lineage under immutable source root")

    original_probe = probe(original)
    processing_probe = probe(processing)
    duration_delta = abs(original_probe["duration"] - processing_probe["duration"])
    if duration_delta > args.duration_tolerance:
        raise ValueError(
            f"duration mismatch {duration_delta:.3f}s exceeds tolerance "
            f"{args.duration_tolerance:.3f}s"
        )
    payload = {
        "schema_version": "travel-video-source-lineage/v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source_root),
        "source_relative_path": str(original.relative_to(source_root)),
        "original": {
            "path": str(original),
            "size": original.stat().st_size,
            "quick_fingerprint": quick_fingerprint(original),
            "probe": original_probe,
        },
        "processing_input": {
            "path": str(processing),
            "size": processing.stat().st_size,
            "quick_fingerprint": quick_fingerprint(processing),
            "probe": processing_probe,
        },
        "time_mapping": {
            "kind": "identity",
            "duration_delta_seconds": round(duration_delta, 6),
            "source_in_offset_seconds": 0.0,
        },
    }
    atomic_write(output, payload)
    print(str(output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
