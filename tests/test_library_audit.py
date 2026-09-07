from travel_video.library_audit import audit_library
from travel_video.library import _render_candidate_evidence, _candidate_evidence_text


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
    assert '<details class="candidate-facts">' in rendered
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
