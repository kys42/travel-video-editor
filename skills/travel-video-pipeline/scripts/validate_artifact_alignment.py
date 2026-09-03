#!/usr/bin/env python3
"""Validate that timeline/transcript artifacts match a source lineage sidecar."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lineage", type=Path)
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()

    lineage = load(args.lineage)
    if lineage.get("schema_version") != "travel-video-source-lineage/v1":
        raise ValueError("unsupported source lineage schema")
    expected = [
        (
            Path(item["path"]).expanduser().resolve(),
            item["quick_fingerprint"],
        )
        for item in (lineage["original"], lineage["processing_input"])
    ]
    checked: list[dict[str, Any]] = []
    for artifact_path_arg in args.artifacts:
        artifact_path = artifact_path_arg.expanduser().resolve()
        artifact = load(artifact_path)
        source = artifact.get("source")
        if not isinstance(source, dict) or not source.get("path"):
            raise ValueError(f"artifact has no source identity: {artifact_path}")
        source_resolved = Path(source["path"]).expanduser().resolve()
        matched = next(
            (
                (expected_path, fingerprint)
                for expected_path, fingerprint in expected
                if source_resolved == expected_path
                or (
                    source_resolved.exists()
                    and expected_path.exists()
                    and source_resolved.samefile(expected_path)
                )
            ),
            None,
        )
        if matched is None:
            raise ValueError(
                f"artifact source is outside lineage: {artifact_path} -> {source_resolved}"
            )
        expected_path, expected_fingerprint = matched
        fingerprint = source.get("quick_fingerprint")
        if fingerprint != expected_fingerprint:
            raise ValueError(f"source fingerprint mismatch: {artifact_path}")
        checked.append(
            {
                "artifact": str(artifact_path),
                "schema_version": artifact.get("schema_version"),
                "source": str(expected_path),
                "quick_fingerprint": fingerprint,
            }
        )
    print(
        json.dumps(
            {
                "schema_version": "travel-video-artifact-alignment/v1",
                "lineage": str(args.lineage.expanduser().resolve()),
                "source_relative_path": lineage["source_relative_path"],
                "checked": checked,
                "ok": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
