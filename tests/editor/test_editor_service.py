from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from travel_video.editor.agent import CodexBackend
from travel_video.editor.catalog import TimelineCatalog
from travel_video.editor.contracts import (
    SSE_EVENT_TYPES,
    EditorAgentContract,
    project_root,
)
from travel_video.editor.server import create_editor_app
from travel_video.editor.store import EditStore, RevisionConflictError
from travel_video.editor.evidence import SceneEvidenceService
from travel_video.editor.tools import ToolGateway


def _fixture_project(tmp_path: Path) -> Path:
    library = tmp_path / "library"
    library.mkdir()
    (library / "index.html").write_text("<h1>Edit Desk</h1>", encoding="utf-8")
    timelines = []
    for index, (title, summary, dialogue) in enumerate(
        [
            (
                "푸드코트 주문",
                "푸드트럭 메뉴를 보며 주문한다.",
                "연어 타코 하나 주세요.",
            ),
            (
                "장난스러운 반응",
                "동행자가 카메라를 보며 크게 웃는다.",
                "이거 진짜 웃기다.",
            ),
        ],
        start=1,
    ):
        asset_id = f"asset-{index}"
        frame = tmp_path / f"frame-{index}.jpg"
        Image.new("RGB", (320, 180), (40 * index, 80, 110)).save(frame)
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


def _sse_events(text: str) -> list[tuple[str, dict[str, object]]]:
    event_name = ""
    events: list[tuple[str, dict[str, object]]] = []
    for line in text.splitlines():
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            events.append((event_name, json.loads(line.removeprefix("data: "))))
    return events


def test_contract_matches_gateway_and_event_schema(tmp_path: Path) -> None:
    manifest = _fixture_project(tmp_path)
    catalog = TimelineCatalog(manifest)
    store = EditStore(tmp_path / "state.sqlite3")
    contract = EditorAgentContract.load()
    gateway = ToolGateway(
        catalog,
        store,
        contract,
        SceneEvidenceService(catalog, tmp_path / "evidence"),
    )

    assert contract.contract_id == "travel-video-editor/editor-agent/v1"
    assert contract.revision == 5
    assert len(contract.capabilities) == 7
    assert len(contract.boundaries) == 4
    assert gateway.contract.names == {
        "list_assets",
        "search_scenes",
        "get_scene_evidence",
        "inspect_scene_range",
        "create_edit_proposal",
        "get_edit_proposal",
        "apply_edit_proposal",
        "create_edit",
        "get_edit",
        "apply_edit_operations",
        "show_scene_refs",
        "show_edit_revision",
        "play_source_range",
        "focus_scene",
        "open_inspector_tab",
    }
    planning = contract.decision_policy["planning"]
    assert any(
        "Do not call create_edit, apply_edit_proposal, or apply_edit_operations" in rule
        for rule in planning
    )
    assert any("explicitly says to execute immediately" in rule for rule in planning)
    duration_policy = contract.decision_policy["duration_and_suggestions"]
    assert any("30, 60, and 90 seconds" in rule for rule in duration_policy)
    assert any("current response" in rule for rule in duration_policy)
    assert contract.get("create_edit").approval == "chat_plan_for_new_edit"
    assert (
        contract.get("apply_edit_operations").approval == "chat_plan_for_major_revision"
    )
    assert contract.get("create_edit_proposal").category == "proposal_action"
    assert (
        contract.get("apply_edit_proposal").approval == "chat_confirmation_of_proposal"
    )
    assert set(contract.get("play_source_range").input_schema["properties"]) >= {
        "scene_id",
        "source_in",
        "source_out",
    }
    assert contract.context_policy["limits"]["evidence_frame_count"] == 12
    assert '"planning"' in contract.prompt_json()
    event_contract = json.loads(
        (project_root() / "docs/contracts/agent-events.v1.schema.json").read_text()
    )
    contract_events = {
        item["properties"]["event"]["const"] for item in event_contract["oneOf"]
    }
    assert contract_events == SSE_EVENT_TYPES

    with pytest.raises(ValueError, match="unknown fields"):
        gateway.invoke(
            "inspect_scene_range",
            {"scene_id": "asset-1:G001", "arbitrary_path": "/readonly/source"},
            "session-invalid-tool",
        )
    with pytest.raises(ValueError, match=r"\$arguments\.candidates must be array"):
        gateway.invoke(
            "create_edit_proposal",
            {
                "title": "invalid",
                "objective": "invalid",
                "candidates": {"scene_id": "asset-1:G001"},
            },
            "session-invalid-tool",
        )


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


def test_deep_evidence_builds_bounded_sheet_and_separates_raw_stt(
    tmp_path: Path,
) -> None:
    manifest_path = _fixture_project(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_timeline = Path(manifest["timelines"][0])
    timeline = json.loads(first_timeline.read_text(encoding="utf-8"))
    timeline["segments"][0]["transcript_candidates"]["ko"][0]["avg_logprob"] = float(
        "nan"
    )
    first_timeline.write_text(json.dumps(timeline), encoding="utf-8")
    catalog = TimelineCatalog(manifest_path)
    service = SceneEvidenceService(catalog, tmp_path / "evidence-cache")

    inspection = service.inspect(
        scene_id="asset-1:G001",
        source_in=1,
        source_out=8,
        visual_mode="existing",
        frame_count=8,
        include_raw_stt=True,
        context_seconds=3,
    )

    assert inspection.card["type"] == "scene_deep_evidence"
    assert inspection.card["visual"]["source"] == "analysis_frames"
    assert inspection.card["visual"]["frame_count"] == 1
    assert inspection.card["transcript"]["raw_stt_candidates"][0] == {
        "segment_id": "S001",
        "source_in": 0.0,
        "source_out": 12.0,
        "language": "ko",
        "text": "연어 타코 하나 주세요.",
        "relation": "selected",
        "avg_logprob": None,
        "no_speech_prob": None,
        "provenance": "raw_asr_candidate_unverified",
    }
    json.dumps(inspection.card, allow_nan=False)
    scene_packet = catalog.scene_evidence("asset-1:G001")
    assert (
        scene_packet["segments"][0]["transcript_candidates"]["ko"][0]["avg_logprob"]
        is None
    )
    json.dumps(scene_packet, allow_nan=False)
    assert len(inspection.local_image_paths) == 1
    sheet = Path(inspection.local_image_paths[0])
    assert sheet.is_file()
    assert service.contact_sheet_path(inspection.card["evidence_id"]) == sheet
    with Image.open(sheet) as image:
        assert image.width == 320
        assert image.height == 214

    cached = service.inspect(
        scene_id="asset-1:G001",
        source_in=1,
        source_out=8,
        visual_mode="existing",
        frame_count=8,
        include_raw_stt=False,
    )
    assert cached.card["visual"]["cached"] is True
    with pytest.raises(ValueError, match="inside the reviewed scene"):
        service.inspect(scene_id="asset-1:G001", source_in=0, source_out=13)
    with pytest.raises(KeyError, match="artifact not found"):
        service.contact_sheet_path("../../readonly/source")


def test_deep_evidence_extracts_proxy_frames_once_and_repairs_partial_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _fixture_project(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    proxy_root = tmp_path / "proxies"
    proxy_root.mkdir()
    proxy = proxy_root / "clip-1.mp4"
    proxy.write_bytes(b"proxy-fixture")
    manifest["proxy_root"] = str(proxy_root)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    catalog = TimelineCatalog(manifest_path)
    service = SceneEvidenceService(catalog, tmp_path / "evidence-cache")
    calls = 0

    def fake_extract(
        _: Path, directory: Path, start: float, end: float, frame_count: int
    ) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        rows: list[dict[str, object]] = []
        for index in range(frame_count):
            path = directory / f"proxy-{index}.jpg"
            Image.new("RGB", (480, 270), (30 + index * 10, 60, 90)).save(path)
            rows.append(
                {
                    "path": str(path),
                    "source_time": start + (index + 0.5) * (end - start) / frame_count,
                    "sample_id": None,
                }
            )
        return rows

    monkeypatch.setattr(service, "_extract_proxy_frames", fake_extract)
    first = service.inspect(
        scene_id="asset-1:G001",
        source_in=1,
        source_out=9,
        visual_mode="proxy",
        frame_count=6,
    )

    assert first.card["visual"]["source"] == "proxy"
    assert first.card["visual"]["frame_count"] == 6
    assert first.card["visual"]["cached"] is False
    assert calls == 1
    artifact_id = first.card["evidence_id"]
    sheet = service.contact_sheet_path(artifact_id)
    sheet.unlink()

    repaired = service.inspect(
        scene_id="asset-1:G001",
        source_in=1,
        source_out=9,
        visual_mode="proxy",
        frame_count=6,
    )
    cached = service.inspect(
        scene_id="asset-1:G001",
        source_in=1,
        source_out=9,
        visual_mode="proxy",
        frame_count=6,
    )

    assert repaired.card["visual"]["cached"] is False
    assert cached.card["visual"]["cached"] is True
    assert calls == 2
    assert service.contact_sheet_path(artifact_id).is_file()


def test_proposal_is_reviewable_then_partially_applies_as_revision(
    tmp_path: Path,
) -> None:
    catalog = TimelineCatalog(_fixture_project(tmp_path))
    store = EditStore(tmp_path / "state.sqlite3")
    proposal = store.create_proposal(
        catalog=catalog,
        session_id="session-proposal",
        title="음식과 반응 제안",
        objective="주문 다음에 웃긴 반응을 배치",
        target_duration=14,
        duration_rationale="대화와 반응을 닫는 실제 후보 길이",
        candidates=[
            {
                "scene_id": "asset-1:G001",
                "source_in": 1,
                "source_out": 7,
                "role": "setup",
                "reason": "주문 대화를 보존",
            },
            {
                "scene_id": "asset-2:G001",
                "source_in": 2,
                "source_out": 10,
                "role": "payoff",
                "reason": "웃긴 반응을 보존",
            },
        ],
        created_by="test",
    )

    assert proposal["status"] == "draft"
    assert proposal["estimated_duration"] == 14
    assert store.session("session-proposal")["active_edit_id"] is None
    selected = proposal["candidates"][1]["candidate_id"]
    result = store.apply_proposal(
        catalog=catalog,
        proposal_id=proposal["proposal_id"],
        selected_candidate_ids=[selected],
        created_by="test",
    )

    assert result["proposal"]["status"] == "partially_applied"
    assert result["proposal"]["selected_candidate_ids"] == [selected]
    assert result["edit"]["revision"]["sequence"] == 2
    assert result["edit"]["revision"]["clip_count"] == 1
    assert (
        result["edit"]["revision"]["plan"]["clips"][0]["metadata"]["scene_id"]
        == "asset-2:G001"
    )
    with pytest.raises(ValueError, match="must be draft"):
        store.apply_proposal(
            catalog=catalog,
            proposal_id=proposal["proposal_id"],
            selected_candidate_ids=None,
            created_by="test",
        )


def test_new_edit_proposal_application_rolls_back_as_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = TimelineCatalog(_fixture_project(tmp_path))
    store = EditStore(tmp_path / "state.sqlite3")
    proposal = store.create_proposal(
        catalog=catalog,
        session_id="session-rollback",
        title="원자적 적용 테스트",
        objective="중간 실패가 빈 edit을 남기지 않아야 함",
        candidates=[
            {
                "scene_id": "asset-1:G001",
                "source_in": 1,
                "source_out": 5,
                "role": "setup",
                "reason": "트랜잭션 검증",
            }
        ],
        created_by="test",
    )

    def fail_after_edit_insert(*_: object, **__: object) -> str:
        raise RuntimeError("synthetic apply failure")

    monkeypatch.setattr(store, "_apply_operations", fail_after_edit_insert)
    with pytest.raises(RuntimeError, match="synthetic"):
        store.apply_proposal(
            catalog=catalog,
            proposal_id=proposal["proposal_id"],
            selected_candidate_ids=None,
            created_by="test",
        )

    assert store.get_proposal(proposal["proposal_id"])["status"] == "draft"
    assert store.session("session-rollback")["active_edit_id"] is None
    with store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0


def test_catalog_prefers_reviewed_captions_and_empty_is_authoritative(
    tmp_path: Path,
) -> None:
    manifest_path = _fixture_project(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_path, second_path = (Path(item) for item in manifest["timelines"])

    first = json.loads(first_path.read_text(encoding="utf-8"))
    first_group = first["context_groups"][0]
    first_group["context_review"]["dialogue_summary"] = "구형대화요약문구"
    first_group["reconciled_utterances"][0]["original_text"] = "구형발화문구"
    first["segments"][0]["transcript_candidates"] = {"ko": [{"text": "구형후보문구"}]}
    first_group["reviewed_dialogue"] = {
        "captions": [
            {
                "caption_id": "G001-C001",
                "start": 2.0,
                "end": 4.0,
                "display_text": "연어 타코 두 개 주세요.",
                "language": "ko",
                "confidence": 0.94,
                "review_status": "reviewed",
                "edit_type": "normalized",
            }
        ],
        "utterances": [
            {
                "utterance_id": "G001-U001",
                "start": 2.0,
                "end": 4.0,
                "original_text": "연어 타코 둘 주세요.",
                "language": "ko",
            }
        ],
    }
    first_path.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")

    second = json.loads(second_path.read_text(encoding="utf-8"))
    second["context_groups"][0]["reviewed_dialogue"] = {
        "captions": [],
        "utterances": [],
    }
    second_path.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")

    catalog = TimelineCatalog(manifest_path)
    results = catalog.search_scenes(query="연어 타코 두 개", limit=4)

    assert len(results) == 1
    assert results[0]["dialogue_excerpt"] == "연어 타코 두 개 주세요."
    assert catalog.search_scenes(query="구형대화요약문구", limit=4) == []
    assert catalog.search_scenes(query="구형발화문구", limit=4) == []
    assert catalog.search_scenes(query="구형후보문구", limit=4) == []
    evidence = catalog.scene_evidence(results[0]["scene_id"])
    assert evidence["captions"] == [
        {
            "caption_id": "G001-C001",
            "source_start": 2.0,
            "source_end": 4.0,
            "language": "ko",
            "display_text": "연어 타코 두 개 주세요.",
            "edit_type": "normalized",
            "confidence": 0.94,
            "review_status": "reviewed",
        }
    ]
    assert evidence["utterances"] == []
    assert catalog.scene("asset-2:G001").compact()["dialogue_excerpt"] == ""
    assert catalog.search_scenes(query="이거 진짜 웃기다", limit=4) == []


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
        },
        limits=EditorAgentContract.load().context_policy["limits"],
    )

    assert context["schema_version"] == "editor-ui-context/v1"
    project = context["project"]
    assert project["title"] == "fixture"
    assert project["asset_count"] == 2
    assert project["scene_count"] == 2
    assert project["total_duration"] == 60.0
    assert project["capture_date_range"] == {
        "start": "2026-08-25",
        "end": "2026-08-25",
    }
    assert project["capture_day_count"] == 1
    assert [item["title"] for item in project["asset_index"]] == [
        "푸드코트 주문",
        "장난스러운 반응",
    ]
    assert project["asset_index_truncated"] is False
    assert project["day_rollups"][0]["asset_count"] == 2
    assert project["timeline_order"] == "capture_time_asc"
    workspace = context["workspace"]
    assert workspace["current_asset"]["title"] == "장난스러운 반응"
    assert [item["title"] for item in workspace["nearby_assets"]] == [
        "푸드코트 주문",
        "장난스러운 반응",
    ]
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
        catalog.resolve_ui_context(
            {"selected_scene_ids": ["missing:G001"]},
            limits=EditorAgentContract.load().context_policy["limits"],
        )


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
    card = ToolGateway(
        catalog,
        store,
        EditorAgentContract.load(),
        SceneEvidenceService(catalog, tmp_path / "evidence"),
    ).invoke(
        "show_edit_revision",
        {
            "edit_id": created["edit_id"],
            "revision_id": changed["revision"]["revision_id"],
        },
        "session-test",
    )
    assert card.payload["clips"][0]["source_in"] == 1.0
    assert card.payload["clips"][0]["source_out"] == 7.5
    assert card.payload["clips"][0]["duration"] == 6.5
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


def test_play_source_range_preserves_and_validates_explicit_bounds(
    tmp_path: Path,
) -> None:
    catalog = TimelineCatalog(_fixture_project(tmp_path))
    gateway = ToolGateway(
        catalog,
        EditStore(tmp_path / "state.sqlite3"),
        EditorAgentContract.load(),
        SceneEvidenceService(catalog, tmp_path / "evidence"),
    )

    result = gateway.invoke(
        "play_source_range",
        {
            "scene_id": "asset-1:G001",
            "source_in": 2.5,
            "source_out": 5.75,
            "autoplay": False,
        },
        "session-playback",
    )

    assert result.payload["source_in"] == 2.5
    assert result.payload["source_out"] == 5.75
    assert result.payload["autoplay"] is False
    with pytest.raises(ValueError, match="inside the reviewed scene"):
        gateway.invoke(
            "play_source_range",
            {
                "scene_id": "asset-1:G001",
                "source_in": 2.5,
                "source_out": 15,
            },
            "session-playback",
        )


def test_demo_agent_proposes_then_applies_revision_over_sse(tmp_path: Path) -> None:
    app = create_editor_app(
        _fixture_project(tmp_path), tmp_path / "state", agent_backend="demo"
    )
    client = TestClient(app)

    assert client.get("/health").json()["catalog"] == {"assets": 2, "scenes": 2}
    planning = client.post(
        "/api/agent/chat",
        json={"message": "음식과 웃긴 반응으로 30초 하이라이트 만들어줘"},
    )

    assert planning.status_code == 200
    assert "event: card" in planning.text
    assert '"type":"edit_proposal_ref"' in planning.text
    assert '"type":"edit_revision_ref"' not in planning.text
    planning_events = _sse_events(planning.text)
    planning_text = "".join(
        str(data.get("content", "")) for name, data in planning_events if name == "text"
    )
    assert "아직 revision은 생성하지 않았습니다" in planning_text
    planning_done = next(data for name, data in planning_events if name == "done")
    proposal_card = next(
        data
        for name, data in planning_events
        if name == "card" and data.get("type") == "edit_proposal_ref"
    )
    assert planning_done["edit_id"] is None
    assert proposal_card["status"] == "draft"

    approval = client.post(
        "/api/agent/chat",
        json={
            "message": "좋아, 이 제안 전체 적용",
            "session_id": planning_done["session_id"],
        },
    )
    assert '"type":"edit_revision_ref"' in approval.text
    assert "event: done" in approval.text
    assert '"backend":"demo"' in approval.text
    approval_done = next(
        data for name, data in _sse_events(approval.text) if name == "done"
    )
    edit = client.get(f"/api/edits/{approval_done['edit_id']}").json()
    assert edit["revision"]["clip_count"] == 2
    assert (
        app.state.store.session(planning_done["session_id"])["active_proposal_id"]
        is None
    )
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/").text == "<h1>Edit Desk</h1>"
    rough_cut = client.get("/rough-cut")
    assert rough_cut.status_code == 200
    assert rough_cut.text == client.get("/").text
    guide = client.get("/guide")
    assert guide.status_code == 200
    assert "data-capabilities" in guide.text
    assert "data-context-levels" in guide.text
    assert "04 · PLAN" in guide.text
    assert "확인 후 revision 생성" in guide.text
    assert "60초 초안 요청" not in guide.text
    assert "fetch('/api/contracts/agent')" in guide.text
    assert 'data-filter="proposal_action"' in guide.text
    assert "chat_confirmation_of_proposal: 'CONFIRM'" in guide.text
    live_contract = client.get("/api/contracts/agent").json()
    assert live_contract["schema_version"] == "editor-agent-contract/v1"
    assert live_contract["contract_id"] == "travel-video-editor/editor-agent/v1"
    assert live_contract["revision"] == 5
    assert len(live_contract["decision_policy"]["planning"]) == 7
    assert live_contract["context_policy"]["limits"]["selected_scene_count"] == 24
    assert set(live_contract["event_contract"]["events"]) == SSE_EVENT_TYPES
    assert len(live_contract["capabilities"]) == 7
    assert client.get("/api/contracts/tools").json() == live_contract
    assert live_contract["identity"]["name"] == "AI Editor"

    inspection = app.state.evidence.inspect(
        scene_id="asset-1:G001",
        source_in=1,
        source_out=8,
        visual_mode="existing",
    )
    sheet_response = client.get(inspection.card["visual"]["contact_sheet_url"])
    assert sheet_response.status_code == 200
    assert sheet_response.headers["content-type"] == "image/jpeg"
    assert "immutable" in sheet_response.headers["cache-control"]


def test_demo_agent_derives_unspecified_duration_from_scene_evidence(
    tmp_path: Path,
) -> None:
    app = create_editor_app(
        _fixture_project(tmp_path), tmp_path / "state", agent_backend="demo"
    )
    client = TestClient(app)

    response = client.post(
        "/api/agent/chat",
        json={"message": "이 영상들로 자연스러운 길이의 하이라이트를 만들어줘"},
    )

    events = _sse_events(response.text)
    done = next(data for name, data in events if name == "done")
    proposal = next(
        data
        for name, data in events
        if name == "card" and data.get("type") == "edit_proposal_ref"
    )

    assert done["edit_id"] is None
    assert proposal["target_duration"] == 18.0
    assert proposal["estimated_duration"] == 18.0


def test_demo_deep_review_does_not_turn_a_highlight_reference_into_an_edit(
    tmp_path: Path,
) -> None:
    app = create_editor_app(
        _fixture_project(tmp_path), tmp_path / "state", agent_backend="demo"
    )
    response = TestClient(app).post(
        "/api/agent/chat",
        json={"message": "첫 하이라이트 장면을 이미지 시트와 원문까지 자세히 봐줘"},
    )

    events = _sse_events(response.text)

    assert any(
        name == "card" and data.get("type") == "scene_deep_evidence"
        for name, data in events
    )
    assert not any(
        name == "card" and data.get("type") == "edit_proposal_ref"
        for name, data in events
    )
    assert next(data for name, data in events if name == "done")["edit_id"] is None


def test_proposal_api_applies_selected_candidates_and_updates_session(
    tmp_path: Path,
) -> None:
    app = create_editor_app(
        _fixture_project(tmp_path), tmp_path / "state", agent_backend="demo"
    )
    client = TestClient(app)
    created = client.post(
        "/api/proposals",
        json={
            "session_id": "session-api",
            "title": "부분 적용 제안",
            "objective": "반응 장면을 먼저 검토",
            "candidates": [
                {
                    "scene_id": "asset-1:G001",
                    "source_in": 1,
                    "source_out": 5,
                    "role": "setup",
                    "reason": "주문 맥락",
                },
                {
                    "scene_id": "asset-2:G001",
                    "source_in": 3,
                    "source_out": 9,
                    "role": "payoff",
                    "reason": "반응 보존",
                },
            ],
        },
    )

    assert created.status_code == 201
    payload = created.json()
    proposal = payload["proposal"]
    assert payload["card"]["type"] == "edit_proposal_ref"
    selected = proposal["candidates"][1]["candidate_id"]
    applied = client.post(
        f"/api/proposals/{proposal['proposal_id']}/apply",
        json={
            "selected_candidate_ids": [selected],
        },
    )

    assert applied.status_code == 200
    assert applied.json()["proposal"]["status"] == "partially_applied"
    assert applied.json()["edit"]["revision"]["clip_count"] == 1
    assert (
        app.state.store.session("session-api")["active_edit_id"]
        == applied.json()["edit"]["edit_id"]
    )
    assert app.state.store.session("session-api")["active_proposal_id"] is None


def test_codex_backend_attaches_each_evidence_image_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_inputs: list[list[object]] = []

    class TextInput:
        def __init__(self, text: str):
            self.text = text

    class LocalImageInput:
        def __init__(self, path: str):
            self.path = path

    class FakeThread:
        id = "thread-test"

        async def run(self, input_items: list[object], **_: object) -> object:
            run_inputs.append(input_items)
            return types.SimpleNamespace(
                final_response=json.dumps(
                    {
                        "response": "확인했습니다.",
                        "tool_calls": [],
                        "suggestions": [],
                        "done": True,
                    }
                )
            )

    class AsyncCodex:
        async def __aenter__(self) -> "AsyncCodex":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def thread_start(self, **_: object) -> FakeThread:
            return FakeThread()

        async def thread_resume(self, *_: object, **__: object) -> FakeThread:
            return FakeThread()

    fake_module = types.ModuleType("openai_codex")
    fake_module.ApprovalMode = types.SimpleNamespace(deny_all="deny_all")
    fake_module.AsyncCodex = AsyncCodex
    fake_module.LocalImageInput = LocalImageInput
    fake_module.Sandbox = types.SimpleNamespace(read_only="read_only")
    fake_module.TextInput = TextInput
    monkeypatch.setitem(sys.modules, "openai_codex", fake_module)

    store = EditStore(tmp_path / "state.sqlite3")
    backend = object.__new__(CodexBackend)
    backend.store = store
    backend.contract = EditorAgentContract.load()
    backend.project_root = tmp_path
    backend.model = "test-model"
    backend.developer_instructions = "test"
    backend._delivered_image_artifacts = {}
    observations = [
        {
            "tool": "inspect_scene_range",
            "ok": True,
            "result": {"evidence_id": "evidence-1"},
            "attachments": [
                {
                    "type": "local_image",
                    "artifact_id": "evidence-1",
                    "path": str(tmp_path / "sheet.jpg"),
                }
            ],
        }
    ]

    asyncio.run(
        backend.decide(
            session_id="session-image",
            message="이미지 확인",
            context={},
            observations=observations,
        )
    )
    asyncio.run(
        backend.decide(
            session_id="session-image",
            message="계속",
            context={},
            observations=observations,
        )
    )

    assert sum(isinstance(item, LocalImageInput) for item in run_inputs[0]) == 1
    assert sum(isinstance(item, LocalImageInput) for item in run_inputs[1]) == 0
    assert "attachments" not in run_inputs[0][0].text
