from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from travel_video.editor.catalog import TimelineCatalog
from travel_video.editor.contracts import SSE_EVENT_TYPES, ToolCatalog, project_root
from travel_video.editor.server import create_editor_app
from travel_video.editor.store import EditStore, RevisionConflictError
from travel_video.editor.tools import ToolGateway


def _fixture_project(tmp_path: Path) -> Path:
    library = tmp_path / "library"
    library.mkdir()
    (library / "index.html").write_text("<h1>Edit Desk</h1>", encoding="utf-8")
    timelines = []
    for index, (title, summary, dialogue) in enumerate(
        [
            ("푸드코트 주문", "푸드트럭 메뉴를 보며 주문한다.", "연어 타코 하나 주세요."),
            ("장난스러운 반응", "동행자가 카메라를 보며 크게 웃는다.", "이거 진짜 웃기다."),
        ],
        start=1,
    ):
        asset_id = f"asset-{index}"
        frame = tmp_path / f"frame-{index}.jpg"
        frame.write_bytes(b"frame")
        timeline = {
            "schema_version": "phase1-video-summarized-timeline/v1",
            "asset_id": asset_id,
            "source": {
                "name": f"clip-{index}.MP4",
                "path": f"/readonly/source/clip-{index}.MP4",
            },
            "media": {
                "duration": 30.0,
                "creation_time": f"2026-08-25T22:0{index}:00Z",
            },
            "samples": [
                {
                    "sample_id": "F001",
                    "time": 2.0,
                    "timecode": "00:02.000",
                    "frame": str(frame),
                }
            ],
            "segments": [
                {
                    "segment_id": "S001",
                    "start": 0.0,
                    "end": 12.0,
                    "review": {"visual_summary": summary, "actions": ["대화", "반응"]},
                    "transcript_candidates": {"ko": [{"text": dialogue}]},
                }
            ],
            "context_groups": [
                {
                    "group_id": "G001",
                    "start": 0.0,
                    "end": 12.0,
                    "label": title,
                    "segment_ids": ["S001"],
                    "context_review": {
                        "narrative_summary": summary,
                        "dialogue_summary": dialogue,
                        "representative_sample_id": "F001",
                        "representative_reason": "행동이 가장 분명함",
                        "key_moments": [{"sample_id": "F001", "role": "대표"}],
                        "notable_moments": [
                            {
                                "sample_id": "F001",
                                "category": "candid",
                                "title": title,
                                "description": summary,
                                "edit_hint": "짧게 살린다.",
                            }
                        ],
                        "confidence": 0.92,
                    },
                    "reconciled_utterances": [
                        {
                            "source_start": 2.0,
                            "source_end": 4.0,
                            "language": "ko",
                            "original_text": dialogue,
                            "confidence": 0.9,
                        }
                    ],
                }
            ],
            "video_summary": {
                "title": title,
                "one_line_summary": summary,
                "narrative_summary": summary,
                "chronological_events": [
                    {"group_id": "G001", "headline": title, "description": summary}
                ],
                "highlight_group_ids": ["G001"],
                "representative_sample_id": "F001",
                "tags": ["음식", "반응"],
            },
        }
        path = tmp_path / f"timeline-{index}.json"
        path.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
        timelines.append(str(path))
    manifest = library / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "phase1-video-library/v1",
                "title": "fixture",
                "timelines": timelines,
                "proxy_root": None,
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_contract_matches_gateway_and_event_schema(tmp_path: Path) -> None:
    manifest = _fixture_project(tmp_path)
    catalog = TimelineCatalog(manifest)
    store = EditStore(tmp_path / "state.sqlite3")
    tools = ToolCatalog.load()
    gateway = ToolGateway(catalog, store, tools)

    assert len(tools.capabilities) == 6
    assert len(tools.boundaries) == 4
    assert gateway.tools.names == {
        "list_assets",
        "search_scenes",
        "get_scene_evidence",
        "create_edit",
        "get_edit",
        "apply_edit_operations",
        "show_scene_refs",
        "show_edit_revision",
        "play_source_range",
        "focus_scene",
        "open_inspector_tab",
    }
    event_contract = json.loads(
        (project_root() / "docs/contracts/agent-events.v1.schema.json").read_text()
    )
    contract_events = {
        item["properties"]["event"]["const"] for item in event_contract["oneOf"]
    }
    assert contract_events == SSE_EVENT_TYPES


def test_catalog_search_is_compact_and_evidence_is_on_demand(tmp_path: Path) -> None:
    catalog = TimelineCatalog(_fixture_project(tmp_path))

    results = catalog.search_scenes(query="연어 타코 주문", limit=4)

    assert len(results) == 1
    assert results[0]["title"] == "푸드코트 주문"
    assert "source" not in results[0]
    assert "segments" not in results[0]
    evidence = catalog.scene_evidence(results[0]["scene_id"])
    assert evidence["utterances"][0]["original_text"] == "연어 타코 하나 주세요."
    assert evidence["segments"][0]["actions"] == ["대화", "반응"]
    assert evidence["segments"][0]["transcript_candidates"] == {
        "ko": [{"text": "연어 타코 하나 주세요."}]
    }


def test_ui_context_resolves_focus_and_cross_asset_selection(tmp_path: Path) -> None:
    catalog = TimelineCatalog(_fixture_project(tmp_path))

    context = catalog.resolve_ui_context(
        {
            "current_asset_id": "asset-2",
            "focused_scene_id": "asset-2:G001",
            "selected_scene_ids": ["asset-1:G001", "asset-2:G001"],
            "visible_asset_ids": ["asset-1", "asset-2"],
            "search_query": "반응",
            "selected_range": {"source_in": 2, "source_out": 8},
            "inspector_tab": "ai",
        }
    )

    assert context["schema_version"] == "editor-ui-context/v1"
    assert context["project"] == {
        "title": "fixture",
        "asset_count": 2,
        "scene_count": 2,
        "timeline_order": "capture_time_asc",
    }
    workspace = context["workspace"]
    assert workspace["current_asset"]["title"] == "장난스러운 반응"
    assert workspace["focused_scene"]["scene_id"] == "asset-2:G001"
    assert [item["scene_id"] for item in workspace["selected_scenes"]] == [
        "asset-1:G001",
        "asset-2:G001",
    ]
    assert workspace["selected_asset_count"] == 2
    assert workspace["selection_is_explicit"] is True
    assert "segments" not in workspace["selected_scenes"][0]
    assert "source" not in workspace["current_asset"]


def test_ui_context_rejects_unknown_scene(tmp_path: Path) -> None:
    catalog = TimelineCatalog(_fixture_project(tmp_path))

    with pytest.raises(KeyError, match="scene not found"):
        catalog.resolve_ui_context({"selected_scene_ids": ["missing:G001"]})


def test_edit_operations_create_immutable_revision_and_reject_stale_write(
    tmp_path: Path,
) -> None:
    catalog = TimelineCatalog(_fixture_project(tmp_path))
    store = EditStore(tmp_path / "state.sqlite3")
    created = store.create_edit(
        title="음식 하이라이트", brief="주문과 반응", target_duration=30
    )
    scene = catalog.search_scenes(query="주문")[0]

    changed = store.apply_operations(
        catalog=catalog,
        edit_id=created["edit_id"],
        expected_revision_id=created["head_revision_id"],
        summary="첫 장면 추가",
        operations=[
            {
                "op": "add_scene",
                "scene_id": scene["scene_id"],
                "source_in": 1.0,
                "source_out": 7.5,
                "reason": "주문 대사 보존",
            }
        ],
        created_by="test",
    )

    assert changed["revision"]["sequence"] == 2
    assert changed["revision"]["timeline_duration"] == 6.5
    assert changed["revision"]["plan"]["clips"][0]["source"].startswith(
        "/readonly/source/"
    )
    previous = store.get_edit(created["edit_id"], created["head_revision_id"])
    assert previous["revision"]["clip_count"] == 0
    with pytest.raises(RevisionConflictError):
        store.apply_operations(
            catalog=catalog,
            edit_id=created["edit_id"],
            expected_revision_id=created["head_revision_id"],
            summary="stale",
            operations=[{"op": "set_title", "title": "충돌"}],
            created_by="test",
        )


def test_demo_agent_runs_search_cards_and_revision_over_sse(tmp_path: Path) -> None:
    app = create_editor_app(
        _fixture_project(tmp_path), tmp_path / "state", agent_backend="demo"
    )
    client = TestClient(app)

    assert client.get("/health").json()["catalog"] == {"assets": 2, "scenes": 2}
    response = client.post(
        "/api/agent/chat",
        json={"message": "음식과 웃긴 반응으로 30초 하이라이트 만들어줘"},
    )

    assert response.status_code == 200
    assert "event: card" in response.text
    assert '"type":"scene_refs"' in response.text
    assert '"type":"edit_revision_ref"' in response.text
    assert "event: done" in response.text
    assert '"backend":"demo"' in response.text
    assert client.get("/openapi.json").status_code == 200
    guide = client.get("/guide")
    assert guide.status_code == 200
    assert 'data-capabilities' in guide.text
    live_contract = client.get("/api/contracts/tools").json()
    assert len(live_contract["capabilities"]) == 6
    assert live_contract["identity"]["name"] == "AI Editor"
