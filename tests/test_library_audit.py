from travel_video.library_audit import audit_library
from travel_video.library import (
    _render_candidate_evidence,
    _candidate_evidence_text,
    _candidate_listing_signals,
)


def test_audit_routes_coarse_and_repeated_results_without_changing_beats():
    beats = [{"summary": "same"}, {"summary": "same"}]
    timeline = {
        "context_groups": [
            {
                "group_id": "G1",
                "start": 0,
                "end": 80,
                "editorial_beats": [{"summary": "continuous shot"}],
                "context_review": {"narrative_summary": "short"},
            },
            {"group_id": "G2", "start": 80, "end": 90, "editorial_beats": beats},
        ]
    }
    report = audit_library(timeline)
    assert report["status"] == "review_required"
    assert report["groups"][0]["review_signals"] == [
        "long_group_without_subdivision",
        "short_scene_description",
    ]
    assert report["groups"][1]["review_signals"] == ["repeated_beat_summary"]
    assert timeline["context_groups"][1]["editorial_beats"] is beats
    assert report["groups"][0]["unassessed_candidate_fields"]["audio"] == 1


def test_short_continuous_clip_is_not_forced_to_split():
    report = audit_library(
        {
            "context_groups": [
                {
                    "group_id": "G1",
                    "start": 0,
                    "end": 4,
                    "editorial_beats": [{"summary": "pan"}],
                }
            ]
        }
    )
    assert report["status"] == "no_heuristic_flags"
    assert report["summary"]["candidate_count"] == 1


def test_rich_evidence_preserves_modality_and_escapes_claims():
    candidate = {
        "evidence": {
            "subjects": [
                {
                    "start": 1,
                    "end": 3,
                    "basis": "speech",
                    "description": "<script>bear</script> mentioned",
                }
            ],
            "people": [{"person_id": "P1", "role": "unknown"}],
        }
    }
    rendered = _render_candidate_evidence(candidate)
    assert "발화 근거" in rendered
    assert "&lt;script&gt;bear&lt;/script&gt;" in rendered
    assert "<script>" not in rendered
    assert "역할 미확인" in rendered
    assert "미평가 / 확인할 근거 부족" not in rendered
    assert 'aria-label="클립 관찰 정보"' in rendered
    assert "<details" not in rendered
    assert _candidate_evidence_text(candidate) == [
        "<script>bear</script> mentioned",
        "P1 unknown 역할 미확인",
    ]


def test_audit_detects_template_filler_even_with_different_titles():
    prefix = "This scene shows a reusable moment with the same generic context and "
    suffix = " and the viewer can inspect its action and reaction later in the editor."
    beats = [
        {
            "summary": prefix + label + suffix,
            "clip_evidence": {"steps": [{"description": "The person reacts."}]},
        }
        for label in ("menu", "food", "drink")
    ]
    report = audit_library(
        {
            "context_groups": [
                {"group_id": "G1", "start": 0, "end": 60, "editorial_beats": beats}
            ]
        }
    )
    flags = report["groups"][0]["review_signals"]
    assert "near_duplicate_beat_summary" in flags
    assert "repeated_evidence_across_beats" in flags


def test_list_signals_distinguish_mentions_from_visible_subjects_and_unknown_people():
    candidate = {
        "dialogue": "곰 이야기",
        "review_reasons": [],
        "evidence": {
            "subjects": [{"kind": "wildlife", "basis": "speech"}],
            "people": [],
        },
    }
    signals = _candidate_listing_signals(candidate)
    assert ("subject", "동물 언급") in signals
    assert ("subject", "동물") not in signals
    assert not any(kind == "people" for kind, _ in signals)
    assert ("dialogue", "대화") in signals


def test_evidence_does_not_repeat_already_visible_dialogue():
    candidate = {
        "dialogue": "소스가 새콤하다",
        "recommended_range": {"start": 0, "end": 2},
        "evidence": {
            "steps": [
                {
                    "description": "발화 내용: 소스가 새콤하다",
                    "start": 0,
                    "end": 2,
                    "basis": "speech",
                }
            ]
        },
    }
    assert _render_candidate_evidence(candidate) == ""


def test_repeated_dialogue_preserves_visual_and_more_precise_evidence():
    candidate = {
        "dialogue": "치킨이 보인다",
        "recommended_range": {"start": 0, "end": 5},
        "evidence": {
            "steps": [
                {"description": "치킨이 보인다", "start": 0, "end": 5, "basis": "visual"},
                {"description": "치킨이 보인다", "start": 2, "end": 4, "basis": "speech"},
            ]
        },
    }
    rendered = _render_candidate_evidence(candidate)
    assert "basis-visual" in rendered
    assert "basis-speech" in rendered
    assert "00:02–00:04" in rendered
