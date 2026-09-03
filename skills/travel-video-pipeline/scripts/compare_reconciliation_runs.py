#!/usr/bin/env python3
"""Compare transcript-review runs by aligned window IDs."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))


def normalized_text(window: dict[str, Any]) -> str:
    return " ".join(
        str(item.get("original_text", "")).strip()
        for item in window.get("utterances", [])
        if str(item.get("original_text", "")).strip()
    ).casefold()


def language_sequence(window: dict[str, Any]) -> list[str]:
    return [str(item.get("language")) for item in window.get("utterances", [])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if len(args.runs) < 2:
        parser.error("provide at least two runs")

    loaded = [(path.expanduser().resolve(), load(path)) for path in args.runs]
    maps = [
        {window["window_id"]: window for window in run.get("windows", [])}
        for _, run in loaded
    ]
    all_ids = sorted({window_id for mapping in maps for window_id in mapping})
    comparisons: list[dict[str, Any]] = []
    full_coverage_count = 0
    language_agreement_count = 0

    for window_id in all_ids:
        windows = [mapping.get(window_id) for mapping in maps]
        coverage = [window is not None for window in windows]
        full_coverage = all(coverage)
        if full_coverage:
            full_coverage_count += 1
        sequences = [language_sequence(window or {}) for window in windows]
        language_agreement = full_coverage and all(
            sequence == sequences[0] for sequence in sequences[1:]
        )
        if language_agreement:
            language_agreement_count += 1
        texts = [normalized_text(window or {}) for window in windows]
        similarities = [
            round(difflib.SequenceMatcher(None, texts[0], text).ratio(), 4)
            if full_coverage
            else None
            for text in texts[1:]
        ]
        comparisons.append(
            {
                "window_id": window_id,
                "coverage": coverage,
                "utterance_counts": [
                    len((window or {}).get("utterances", [])) for window in windows
                ],
                "language_sequences": sequences,
                "language_agreement": language_agreement,
                "text_similarity_to_first": similarities,
                "needs_review": not language_agreement
                or any(value is None or value < 0.8 for value in similarities),
            }
        )

    report = {
        "schema_version": "transcript-reconciliation-comparison/v1",
        "runs": [str(path) for path, _ in loaded],
        "summary": {
            "window_count": len(all_ids),
            "full_coverage_count": full_coverage_count,
            "language_agreement_count": language_agreement_count,
            "needs_review_count": sum(item["needs_review"] for item in comparisons),
        },
        "windows": comparisons,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
