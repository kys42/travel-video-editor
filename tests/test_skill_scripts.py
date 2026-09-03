from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = ROOT / "skills/travel-video-pipeline/scripts"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_merge_reconciliation_shards_orders_and_validates(tmp_path: Path) -> None:
    packet = {
        "schema_version": "transcript-reconciliation-packet/v1",
        "windows": [
            {
                "window_id": "RW0001",
                "start": 0.0,
                "end": 2.0,
                "apple_candidates": {
                    "ko-KR": {"source_candidate_ids": ["KO-1"]}
                },
            },
            {
                "window_id": "RW0002",
                "start": 2.0,
                "end": 4.0,
                "apple_candidates": {
                    "en-US": {"source_candidate_ids": ["EN-1"]}
                },
            },
        ],
    }
    packet_path = tmp_path / "packet.json"
    write_json(packet_path, packet)
    shard_two = {
        "schema_version": "transcript-reconciliation-shard/v1",
        "packet": str(packet_path),
        "windows": [
            {
                "window_id": "RW0002",
                "utterances": [
                    {
                        "start": 2.1,
                        "end": 3.0,
                        "language": "en",
                        "original_text": "Thanks",
                        "source_candidate_ids": ["EN-1"],
                    }
                ],
            }
        ],
    }
    shard_one = {
        "schema_version": "transcript-reconciliation-shard/v1",
        "packet": str(packet_path),
        "windows": [
            {
                "window_id": "RW0001",
                "utterances": [
                    {
                        "start": 0.2,
                        "end": 1.0,
                        "language": "ko",
                        "original_text": "고마워",
                        "source_candidate_ids": ["KO-1"],
                    }
                ],
            }
        ],
    }
    shard_one_path = tmp_path / "one.json"
    shard_two_path = tmp_path / "two.json"
    output = tmp_path / "review.json"
    write_json(shard_one_path, shard_one)
    write_json(shard_two_path, shard_two)

    subprocess.run(
        [
            sys.executable,
            str(SKILL_SCRIPTS / "merge_reconciliation_shards.py"),
            str(packet_path),
            str(shard_two_path),
            str(shard_one_path),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    review = json.loads(output.read_text(encoding="utf-8"))
    assert review["schema_version"] == "transcript-reconciliation-review/v1"
    assert [window["window_id"] for window in review["windows"]] == [
        "RW0001",
        "RW0002",
    ]


def test_compare_reconciliation_runs_flags_disagreement(tmp_path: Path) -> None:
    first = {
        "windows": [
            {
                "window_id": "RW0001",
                "utterances": [{"language": "ko", "original_text": "안녕"}],
            }
        ]
    }
    second = {
        "windows": [
            {
                "window_id": "RW0001",
                "utterances": [{"language": "en", "original_text": "hello"}],
            }
        ]
    }
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    output = tmp_path / "comparison.json"
    write_json(first_path, first)
    write_json(second_path, second)

    subprocess.run(
        [
            sys.executable,
            str(SKILL_SCRIPTS / "compare_reconciliation_runs.py"),
            str(first_path),
            str(second_path),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["needs_review_count"] == 1
    assert report["windows"][0]["needs_review"] is True
