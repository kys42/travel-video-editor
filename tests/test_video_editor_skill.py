from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def load_renderer():
    script = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "video-editor"
        / "scripts"
        / "render_edit.py"
    )
    spec = importlib.util.spec_from_file_location("video_editor_render_edit", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_caption_helper():
    script = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "video-editor"
        / "scripts"
        / "attach_timeline_captions.py"
    )
    spec = importlib.util.spec_from_file_location("video_editor_caption_helper", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_probe(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "duration": 30.0,
        "size_bytes": 100_000,
        "format_name": "mov,mp4",
        "video": {
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "frame_rate": "30/1",
        },
        "audio": {
            "codec": "aac",
            "sample_rate": 48_000,
            "channels": 2,
        },
    }


def test_atempo_filters_cover_extended_speed_range() -> None:
    renderer = load_renderer()

    assert renderer._atempo_filters(0.25) == [
        "atempo=0.50000000",
        "atempo=0.50000000",
    ]
    assert renderer._atempo_filters(1.0) == []
    assert renderer._atempo_filters(8.0) == [
        "atempo=2.00000000",
        "atempo=2.00000000",
        "atempo=2.00000000",
    ]


def test_normalize_and_build_advanced_edit(monkeypatch, tmp_path: Path) -> None:
    renderer = load_renderer()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    plan_path = tmp_path / "plan.json"
    output_path = tmp_path / "output.mp4"
    font = tmp_path / "font.ttf"
    font.write_bytes(b"font")
    plan = {
        "schema_version": "video-edit-plan/v1",
        "output": {"width": 1080, "height": 1920, "fps": "30"},
        "clips": [
            {
                "id": "C001",
                "source": str(source),
                "source_in": 0,
                "source_out": 4,
                "speed": 4,
                "mute_audio": True,
                "audio_fade_in": 0.1,
                "audio_fade_out": 0.1,
                "reframe": {
                    "mode": "cover",
                    "anchor_x": 0.25,
                    "anchor_y": 0.4,
                },
                "rotation": 90,
                "transition_after": {"type": "dissolve", "duration": 0.25},
            },
            {
                "id": "C002",
                "source": str(source),
                "source_in": 4,
                "source_out": 5,
                "speed": 0.5,
            },
        ],
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(renderer, "probe_media", fake_probe)
    monkeypatch.setattr(renderer, "_encoder_available", lambda _name: True)
    monkeypatch.setattr(renderer, "_resolve_font", lambda _configured: font)

    normalized = renderer.normalize_plan(plan, plan_path, output_path, "source")

    assert normalized["timeline_duration"] == pytest.approx(2.75)
    assert normalized["clips"][0]["output_out"] == pytest.approx(1.0)
    assert normalized["clips"][1]["output_in"] == pytest.approx(0.75)
    assert normalized["clips"][1]["output_out"] == pytest.approx(2.75)

    command = renderer.build_command(normalized, tmp_path / "partial.mp4", [])
    graph = command[command.index("-filter_complex") + 1]
    assert "transpose=clock" in graph
    assert "force_original_aspect_ratio=increase" in graph
    assert "crop=1080:1920" in graph
    assert "atempo=2.00000000,atempo=2.00000000" in graph
    assert "volume=0" in graph
    assert "xfade=transition=fade:duration=0.250000:offset=0.750000" in graph
    assert "acrossfade=d=0.250000" in graph


def test_rejects_transition_after_final_clip(monkeypatch, tmp_path: Path) -> None:
    renderer = load_renderer()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    plan_path = tmp_path / "plan.json"
    output_path = tmp_path / "output.mp4"
    font = tmp_path / "font.ttf"
    font.write_bytes(b"font")
    plan = {
        "schema_version": "video-edit-plan/v1",
        "clips": [
            {
                "id": "C001",
                "source": str(source),
                "source_in": 0,
                "source_out": 2,
                "transition_after": "dissolve",
            }
        ],
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(renderer, "probe_media", fake_probe)
    monkeypatch.setattr(renderer, "_encoder_available", lambda _name: True)
    monkeypatch.setattr(renderer, "_resolve_font", lambda _configured: font)

    with pytest.raises(renderer.PlanError, match="cannot follow the final clip"):
        renderer.normalize_plan(plan, plan_path, output_path, "source")


def test_caption_helper_prefers_dialogue_script(tmp_path: Path) -> None:
    timeline = {
        "dialogue_script": {
            "lines": [
                {
                    "start": 2.0,
                    "end": 5.0,
                    "language": "ko",
                    "original_text": "잘 정리된 대사입니다.",
                    "confidence": 0.95,
                }
            ]
        },
        "reconciled_transcript": {
            "utterances": [
                {
                    "start": 2.0,
                    "end": 5.0,
                    "language": "ko",
                    "original_text": "단 어 별 원 문",
                    "confidence": 0.99,
                }
            ]
        },
    }
    timeline_path = tmp_path / "timeline.scripted.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    plan = {
        "schema_version": "video-edit-plan/v1",
        "clips": [
            {
                "id": "C001",
                "source": "/tmp/source.mp4",
                "source_in": 1.0,
                "source_out": 6.0,
                "metadata": {"timeline_final": str(timeline_path)},
            }
        ],
    }
    plan_path = tmp_path / "plan.json"
    output_path = tmp_path / "captioned.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    script = (
        Path(__file__).resolve().parents[1]
        / "skills/video-editor/scripts/attach_timeline_captions.py"
    )

    subprocess.run(
        [
            sys.executable,
            str(script),
            str(plan_path),
            "--output",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(output_path.read_text(encoding="utf-8"))

    assert [item["text"] for item in result["captions"]] == ["잘 정리된 대사입니다."]
    assert "단 어 별 원 문" not in output_path.read_text(encoding="utf-8")


def test_caption_helper_prefers_only_usable_reviewed_dialogue(tmp_path: Path) -> None:
    timeline = {
        "source": {
            "name": "source-1.mp4",
            "path": "/analysis/source-1.mp4",
            "quick_fingerprint": "fingerprint-1",
        },
        "media": {"duration": 30.0},
        "reviewed_dialogue": {
            "policy": {"policy_version": "dialogue-preservation/v1"},
            "policy_audit": {
                "status": "pass",
                "caption_coverage_ratio": 1.0,
                "uncaptioned_lexical_utterance_ids": [],
            },
            "captions": [
                {
                    "caption_id": "CAP001",
                    "start": 12.0,
                    "end": 14.0,
                    "language": "ko",
                    "display_text": "기차에 타러 갑니다.",
                    "translation": "We are boarding the train.",
                    "confidence": 0.96,
                    "review_status": "verified",
                    "edit_type": "normalized",
                    "source_utterance_ids": ["U001", "U002"],
                },
                {
                    "caption_id": "CAP002",
                    "start": 22.0,
                    "end": 24.0,
                    "language": "en",
                    "display_text": "All aboard now.",
                    "confidence": 0.93,
                    "review_status": "reviewed",
                    "edit_type": "verbatim",
                    "source_candidate_ids": ["APPLE-en-US-009"],
                },
                {
                    "start": 14.0,
                    "end": 15.0,
                    "language": "ko",
                    "display_text": "아직 후보입니다.",
                    "confidence": 0.99,
                    "review_status": "candidate",
                    "edit_type": "normalized",
                },
                {
                    "start": 15.0,
                    "end": 16.0,
                    "language": "ko",
                    "display_text": "신뢰도가 낮습니다.",
                    "confidence": 0.4,
                    "review_status": "verified",
                    "edit_type": "normalized",
                },
                {
                    "start": 16.0,
                    "end": 17.0,
                    "language": "uncertain",
                    "display_text": "언어가 불확실합니다.",
                    "confidence": 0.99,
                    "review_status": "verified",
                    "edit_type": "normalized",
                },
                {
                    "start": 17.0,
                    "end": 18.0,
                    "language": "ko",
                    "display_text": "사용하지 않기로 했습니다.",
                    "confidence": 0.99,
                    "review_status": "verified",
                    "edit_type": "normalized",
                    "usable": False,
                },
            ],
        },
        "dialogue_script": {
            "lines": [
                {
                    "start": 12.0,
                    "end": 14.0,
                    "language": "ko",
                    "original_text": "낮은 단계의 대사는 사용하면 안 됩니다.",
                    "confidence": 0.99,
                }
            ]
        },
        "reconciled_transcript": {
            "utterances": [
                {
                    "start": 12.0,
                    "end": 14.0,
                    "language": "ko",
                    "original_text": "원시 전사도 사용하면 안 됩니다.",
                    "confidence": 0.99,
                }
            ]
        },
    }
    timeline_path = tmp_path / "timeline.reviewed.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    timeline_two = json.loads(json.dumps(timeline))
    timeline_two["source"] = {
        "name": "source-2.mp4",
        "path": "/analysis/source-2.mp4",
        "quick_fingerprint": "fingerprint-2",
    }
    timeline_path_two = tmp_path / "timeline.reviewed-2.json"
    timeline_path_two.write_text(json.dumps(timeline_two), encoding="utf-8")
    plan = {
        "schema_version": "video-edit-plan/v1",
        "clips": [
            {
                "id": "C001",
                "source": "/tmp/source-1.mp4",
                "source_in": 10.0,
                "source_out": 18.0,
                "speed": 2.0,
                "transition_after": {"type": "dissolve", "duration": 1.0},
                "metadata": {
                    "source_relative_path": "source-1.mp4",
                    "timeline_quick_fingerprint": "fingerprint-1",
                    "timeline_final": str(timeline_path),
                },
            },
            {
                "id": "C002",
                "source": "/tmp/source-2.mp4",
                "source_in": 20.0,
                "source_out": 26.0,
                "metadata": {
                    "source_relative_path": "source-2.mp4",
                    "timeline_quick_fingerprint": "fingerprint-2",
                    "timeline_final": str(timeline_path_two),
                },
            },
        ],
    }
    plan_path = tmp_path / "plan.json"
    output_path = tmp_path / "captioned.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    script = (
        Path(__file__).resolve().parents[1]
        / "skills/video-editor/scripts/attach_timeline_captions.py"
    )

    subprocess.run(
        [
            sys.executable,
            str(script),
            str(plan_path),
            "--output",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(output_path.read_text(encoding="utf-8"))

    assert [(item["start"], item["end"]) for item in result["captions"]] == [
        (1.0, 2.0),
        (2.5, 3.0),
        (5.0, 7.0),
    ]
    assert [item["text"] for item in result["captions"]] == [
        "기차에 타러 갑니다.",
        "신뢰도가 낮습니다.",
        "All aboard now.",
    ]
    assert [item["kind"] for item in result["captions"]] == [
        "normalized",
        "normalized",
        "verbatim",
    ]
    assert result["captions"][0]["language"] == "ko"
    assert result["captions"][0]["source_utterance_ids"] == ["U001", "U002"]
    assert result["captions"][2]["source_candidate_ids"] == ["APPLE-en-US-009"]
    assert all(
        item["source_stage"] == "reviewed_dialogue.captions"
        for item in result["captions"]
    )
    rendered_text = output_path.read_text(encoding="utf-8")
    assert "We are boarding the train." not in rendered_text
    assert "낮은 단계의 대사는 사용하면 안 됩니다." not in rendered_text
    assert "원시 전사도 사용하면 안 됩니다." not in rendered_text


def test_caption_helper_rejects_partially_included_reviewed_caption(
    tmp_path: Path,
) -> None:
    timeline_path = tmp_path / "timeline.reviewed.json"
    timeline_path.write_text(
        json.dumps(
            {
                "source": {
                    "name": "source.mp4",
                    "path": "/analysis/source.mp4",
                    "quick_fingerprint": "fingerprint",
                },
                "media": {"duration": 10.0},
                "reviewed_dialogue": {
                    "policy": {"policy_version": "dialogue-preservation/v1"},
                    "policy_audit": {
                        "status": "pass",
                        "caption_coverage_ratio": 1.0,
                        "uncaptioned_lexical_utterance_ids": [],
                    },
                    "captions": [
                        {
                            "caption_id": "CAP001",
                            "start": 2.0,
                            "end": 4.0,
                            "language": "ko",
                            "display_text": "끝까지 들어야 하는 문장입니다.",
                            "confidence": 0.8,
                            "review_status": "reviewed",
                            "edit_type": "normalized",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "video-edit-plan/v1",
                "clips": [
                    {
                        "id": "C001",
                        "source": "/original/source.mp4",
                        "source_in": 1.0,
                        "source_out": 3.0,
                        "metadata": {
                            "source_relative_path": "source.mp4",
                            "timeline_quick_fingerprint": "fingerprint",
                            "timeline_final": str(timeline_path),
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "captioned.json"
    script = (
        Path(__file__).resolve().parents[1]
        / "skills/video-editor/scripts/attach_timeline_captions.py"
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            str(plan_path),
            "--output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "only partially included" in completed.stderr


def test_caption_helper_rejects_mismatched_reviewed_timeline_source() -> None:
    helper = load_caption_helper()
    timeline = {
        "source": {
            "name": "same.mp4",
            "path": "/analysis/0826/same.mp4",
            "quick_fingerprint": "fingerprint",
        },
        "media": {"duration": 10.0},
        "reviewed_dialogue": {"policy": {"policy_version": "dialogue-preservation/v1"}},
    }

    with pytest.raises(ValueError, match="relative path does not match"):
        helper.validate_timeline_identity(
            {
                "source": "/original/0827/same.mp4",
                "source_in": 0.0,
                "source_out": 5.0,
            },
            {"source_relative_path": "0827/same.mp4"},
            timeline,
            Path("/analysis/timeline.json"),
            helper.REVIEWED_DIALOGUE_SOURCE,
        )


def test_caption_helper_requires_matching_timeline_fingerprint() -> None:
    helper = load_caption_helper()
    timeline = {
        "source": {
            "name": "same.mp4",
            "path": "/analysis/0827/same.mp4",
            "quick_fingerprint": "actual-fingerprint",
        },
        "media": {"duration": 10.0},
        "reviewed_dialogue": {"policy": {"policy_version": "dialogue-preservation/v1"}},
    }

    with pytest.raises(ValueError, match="fingerprint does not match"):
        helper.validate_timeline_identity(
            {
                "source": "/original/0827/same.mp4",
                "source_in": 0.0,
                "source_out": 5.0,
            },
            {
                "source_relative_path": "0827/same.mp4",
                "timeline_quick_fingerprint": "stale-fingerprint",
            },
            timeline,
            Path("/analysis/timeline.json"),
            helper.REVIEWED_DIALOGUE_SOURCE,
        )


def test_caption_helper_rejects_failed_preservation_audit() -> None:
    helper = load_caption_helper()
    timeline = {
        "reviewed_dialogue": {
            "policy": {"policy_version": "dialogue-preservation/v1"},
            "policy_audit": {
                "status": "fail",
                "caption_coverage_ratio": 0.5,
                "uncaptioned_lexical_utterance_ids": ["G001-U002"],
            },
            "captions": [],
        }
    }

    with pytest.raises(ValueError, match="require a passing policy audit"):
        helper.caption_source(timeline)


def test_caption_helper_falls_back_to_reconciled_transcript(tmp_path: Path) -> None:
    timeline = {
        "reconciled_transcript": {
            "utterances": [
                {
                    "utterance_id": "U001",
                    "start": 2.0,
                    "end": 4.0,
                    "language": "mixed",
                    "original_text": "다음 stop에서 내려요.",
                    "confidence": 0.91,
                }
            ]
        }
    }
    timeline_path = tmp_path / "timeline.final.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "video-edit-plan/v1",
                "clips": [
                    {
                        "id": "C001",
                        "source": "/tmp/source.mp4",
                        "source_in": 1.0,
                        "source_out": 5.0,
                        "metadata": {"timeline_final": str(timeline_path)},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "captioned.json"
    script = (
        Path(__file__).resolve().parents[1]
        / "skills/video-editor/scripts/attach_timeline_captions.py"
    )

    subprocess.run(
        [
            sys.executable,
            str(script),
            str(plan_path),
            "--output",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(output_path.read_text(encoding="utf-8"))

    assert result["captions"] == [
        {
            "start": 1.0,
            "end": 3.0,
            "text": "다음 stop에서 내려요.",
            "kind": "stt",
            "language": "mixed",
            "source_stage": "reconciled_transcript.utterances",
            "source_timeline": str(timeline_path),
        }
    ]


def test_empty_reviewed_dialogue_does_not_revive_legacy_captions(
    tmp_path: Path,
) -> None:
    timeline = {
        "reviewed_dialogue": {"captions": []},
        "dialogue_script": {
            "lines": [
                {
                    "start": 2.0,
                    "end": 4.0,
                    "language": "ko",
                    "original_text": "검수에서 제외된 문장입니다.",
                    "confidence": 0.99,
                }
            ]
        },
    }
    timeline_path = tmp_path / "timeline.reviewed.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "video-edit-plan/v1",
                "clips": [
                    {
                        "id": "C001",
                        "source": "/tmp/source.mp4",
                        "source_in": 1.0,
                        "source_out": 5.0,
                        "metadata": {"timeline_final": str(timeline_path)},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "captioned.json"
    script = (
        Path(__file__).resolve().parents[1]
        / "skills/video-editor/scripts/attach_timeline_captions.py"
    )

    subprocess.run(
        [
            sys.executable,
            str(script),
            str(plan_path),
            "--output",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(output_path.read_text(encoding="utf-8"))

    assert result["captions"] == []
    assert result["provenance"]["caption_generation"]["source_item_counts"] == {
        "reviewed_dialogue.captions": 0
    }


def test_caption_helper_resolves_reviewed_timeline_root(tmp_path: Path) -> None:
    asset_root = tmp_path / "assets"
    timeline_path = (
        asset_root
        / "DJI_20260101010101_0001_D--abc123"
        / "scene-dialogue"
        / "timeline.dialogue-reviewed.summarized.json"
    )
    timeline_path.parent.mkdir(parents=True)
    timeline_path.write_text(
        json.dumps(
            {
                "reviewed_dialogue": {
                    "captions": [
                        {
                            "caption_id": "CAP001",
                            "start": 2.0,
                            "end": 4.0,
                            "language": "ko",
                            "display_text": "새 검수 대본입니다.",
                            "confidence": 0.97,
                            "review_status": "reviewed",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "video-edit-plan/v1",
                "captions": [
                    {"start": 0.0, "end": 1.0, "text": "legacy", "kind": "stt"}
                ],
                "clips": [
                    {
                        "id": "C001",
                        "source": "/archive/DJI_20260101010101_0001_D.MP4",
                        "source_in": 1.0,
                        "source_out": 5.0,
                        "metadata": {
                            "source_relative_path": (
                                "0101/DJI_20260101010101_0001_D.MP4"
                            ),
                            "timeline_final": "/legacy/timeline.final.json",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "captioned.json"
    script = (
        Path(__file__).resolve().parents[1]
        / "skills/video-editor/scripts/attach_timeline_captions.py"
    )

    subprocess.run(
        [
            sys.executable,
            str(script),
            str(plan_path),
            "--output",
            str(output_path),
            "--timeline-root",
            str(asset_root),
            "--replace-existing",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(output_path.read_text(encoding="utf-8"))

    assert result["clips"][0]["metadata"]["timeline_final"] == str(timeline_path)
    assert [caption["text"] for caption in result["captions"]] == [
        "새 검수 대본입니다."
    ]
    assert (
        result["provenance"]["caption_generation"]["replaced_existing_caption_count"]
        == 1
    )
