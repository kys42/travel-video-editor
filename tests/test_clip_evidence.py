from __future__ import annotations

import copy
import json

import pytest

from travel_video.clip_evidence import (
    SCHEMA,
    build_candidate_library,
    person_observations,
    validate_person_observations,
)
from travel_video.scene_dialogue import (
    build_scene_dialogue_packet,
    merge_scene_dialogue_review,
    merge_scene_dialogue_review_shards,
    slice_scene_dialogue_packet,
    validate_scene_dialogue_review,
)
from test_scene_dialogue import (
    _quick_timeline,
    _apple_transcript,
    _visual_packet,
    _integrated_review,
    _boundary_proposals,
)


def empty_evidence(beat):
    return {
        "schema_version": SCHEMA,
        "core_range": {"start": beat["start"], "end": beat["end"]},
        "event_closure": "unknown",
        **{
            k: []
            for k in (
                "steps",
                "subjects",
                "people",
                "speakers",
                "interactions",
                "audio",
                "quality",
            )
        },
    }


@pytest.fixture
def enriched(tmp_path):
    timeline = tmp_path / "timeline.json"
    apple = tmp_path / "apple.json"
    packet_path = tmp_path / "packet.json"
    timeline.write_text(json.dumps(_quick_timeline(tmp_path)))
    apple.write_text(json.dumps(_apple_transcript()))
    boundaries = _boundary_proposals(tmp_path)
    boundary = json.loads(boundaries.read_text())
    boundary["person_observations"] = person_observations(
        {
            "visualIntervalSeconds": 1 / 3,
            "faceObservations": [
                {
                    "timestamp": 2.0,
                    "boundingBox": [0.1, 0.2, 0.3, 0.4],
                    "confidence": 0.9,
                }
            ],
            "personObservations": [],
        },
        12.0,
    )
    boundaries.write_text(json.dumps(boundary))
    build_scene_dialogue_packet(
        apple,
        timeline,
        packet_path,
        visual_packet_path=_visual_packet(tmp_path),
        boundary_proposals_path=boundaries,
        clip_evidence=True,
    )
    packet = json.loads(packet_path.read_text())
    review = _integrated_review(packet)
    for scene in review["scenes"]:
        for beat in scene["editorial_beats"]:
            beat["source_boundary_proposal_ids"] = []
            beat["clip_evidence"] = empty_evidence(beat)
    data = review["scenes"][0]["editorial_beats"][0]["clip_evidence"]
    data["event_closure"] = "closed"
    data["subjects"] = [
        {
            "kind": "activity",
            "description": "Travel mentioned in dialogue",
            "start": 1.2,
            "end": 2.8,
            "basis": "speech",
            "confidence": 0.8,
            "source_sample_ids": [],
            "source_utterance_ids": ["G001-U001"],
        }
    ]
    data["people"] = [
        {
            "person_id": "P1",
            "role": "traveler",
            "basis": "inferred",
            "confidence": 0.7,
            "source_sample_ids": ["F0001"],
            "source_detection_ids": ["PV-face-000001"],
        }
    ]
    data["speakers"] = [
        {
            "speaker_id": "V1",
            "source_utterance_ids": ["G001-U001"],
            "person_id": None,
            "link_basis": "unknown",
            "confidence": 0.5,
        }
    ]
    return timeline, packet_path, packet, review


def test_enriched_roundtrip_preserves_evidence_and_never_grants_privacy(
    enriched, tmp_path
):
    timeline, packet_path, packet, review = enriched
    validate_scene_dialogue_review(packet, review)
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review))
    output = tmp_path / "merged.json"
    merge_scene_dialogue_review(timeline, packet_path, review_path, output)
    merged = json.loads(output.read_text())
    candidates = build_candidate_library(merged)["candidates"]
    assert candidates[0]["evidence"]["people"][0]["role"] == "traveler"
    assert candidates[0]["privacy"] == {"owners": "unknown", "no_faces": "unknown"}
    assert candidates[1]["readiness"] == "needs_review"
    assert "neighbor_boundary_review" in candidates[1]["review_reasons"]
    # Stable identity, content-sensitive revision; no summary rewrite changes source times.
    merged["reviewed_dialogue"]["editorial_beats"][0]["summary"] = (
        "Changed interpretation"
    )
    updated = build_candidate_library(merged)["candidates"][0]
    assert updated["candidate_id"] == candidates[0]["candidate_id"]
    assert updated["revision"] != candidates[0]["revision"]
    assert updated["recommended_range"] == candidates[0]["recommended_range"]
    merged["reviewed_dialogue"]["captions"][0]["display_text"] = "Partial … words"
    partial = build_candidate_library(merged)["candidates"][0]
    assert "caption_uncertainty" in partial["review_reasons"]
    assert "Partial … words" in partial["dialogue"]


def test_candidate_context_crosses_groups_without_changing_edit_range(
    enriched, tmp_path
):
    timeline, packet_path, _, review = enriched
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review))
    output = tmp_path / "merged.json"
    merge_scene_dialogue_review(timeline, packet_path, review_path, output)
    merged = json.loads(output.read_text())
    # Deliberately unordered source data. The crossing utterance must stay in
    # overlapping evidence, not disappear between previous/next buckets.
    merged["reviewed_dialogue"]["captions"].reverse()
    merged["reviewed_dialogue"]["utterances"].reverse()
    candidates = build_candidate_library(merged)["candidates"]
    first, second = candidates
    assert first["recommended_range"] == {"start": 0, "end": 6}
    assert second["recommended_range"] == {"start": 5.5, "end": 12}
    assert first["context_range"]["end"] >= 6.8
    assert second["context_range"]["start"] <= 1.2
    assert "G002-U001" in [
        u["utterance_id"]
        for u in first["dialogue_context"]["utterances"]["overlapping"]
    ]
    assert "G001-U001" in [
        u["utterance_id"] for u in second["dialogue_context"]["utterances"]["previous"]
    ]
    original_id = first["candidate_id"]
    original_revision = first["revision"]
    # Editing neighboring context changes the content snapshot, not candidate identity.
    for item in merged["reviewed_dialogue"]["utterances"]:
        if item["utterance_id"] == "G002-U002":
            item["original_text"] = "Updated neighboring context"
    updated = build_candidate_library(merged)["candidates"][0]
    assert updated["candidate_id"] == original_id
    assert updated["revision"] != original_revision


def test_dialogue_neighbors_keep_two_chronological_turns_and_edge_overlaps():
    from travel_video.clip_evidence import _dialogue_neighbors

    items = [{"start": i, "end": i + 1, "id": str(i)} for i in range(8)]
    items.append({"start": 3.5, "end": 5.5, "id": "crossing"})
    result = _dialogue_neighbors(list(reversed(items)), 4, 5)
    assert [i["id"] for i in result["previous"]] == ["2", "3"]
    assert [i["id"] for i in result["overlapping"]] == ["crossing", "4"]
    assert [i["id"] for i in result["next"]] == ["5", "6"]
    result["overlapping"][0]["id"] = "edited"
    assert items[-1]["id"] == "crossing"


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda d: d["subjects"][0].update(basis="visual"), "matching modality"),
        (
            lambda d: d["subjects"][0].update(source_utterance_ids=["invented"]),
            "unknown IDs",
        ),
        (lambda d: d["subjects"][0].update(start=4.0, end=5.0), "does not overlap"),
        (lambda d: d["subjects"][0].update(confidence=float("nan")), "finite"),
        (
            lambda d: d["subjects"][0].update(description=None),
            "Invalid clip evidence claim",
        ),
        (
            lambda d: d["subjects"][0].update(description=123),
            "Invalid clip evidence claim",
        ),
        (lambda d: d["core_range"].update(start=2.0), "cuts an utterance"),
        (lambda d: d["people"][0].update(role="owner"), "owner identity"),
        (lambda d: d["people"][0].update(source_detection_ids=["fake"]), "unknown IDs"),
        (
            lambda d: d["speakers"][0].update(person_id="P1", link_basis="verified"),
            "explicitly inferred",
        ),
        (
            lambda d: d.update(audio=[{**d["subjects"][0], "kind": "music"}]),
            "listening evidence",
        ),
    ],
)
def test_unjustified_claims_are_rejected(enriched, mutation, match):
    _, _, packet, review = enriched
    mutation(review["scenes"][0]["editorial_beats"][0]["clip_evidence"])
    with pytest.raises(ValueError, match=match):
        validate_scene_dialogue_review(packet, review)


def test_enriched_contract_is_required_and_preserved_by_slicing(enriched, tmp_path):
    _, path, packet, review = enriched
    sliced = tmp_path / "slice.json"
    slice_scene_dialogue_packet(path, ["G001"], sliced)
    data = json.loads(sliced.read_text())
    assert data["policy"]["clip_evidence_required"]
    assert data["review_contract"]["clip_evidence"]["schema_version"] == SCHEMA
    del review["scenes"][0]["editorial_beats"][0]["clip_evidence"]
    with pytest.raises(ValueError, match="requires clip-editorial-evidence"):
        validate_scene_dialogue_review(packet, review)


def test_namespace_updates_nested_evidence_references(enriched, tmp_path):
    _, path, packet, review = enriched
    # Model IDs can be local before shard merge.
    scene = review["scenes"][0]
    scene["utterances"][0]["utterance_id"] = "U1"
    scene["window_decisions"][0]["source_utterance_ids"] = ["U1"]
    scene["captions"][0]["source_utterance_ids"] = ["U1"]
    beat = scene["editorial_beats"][0]
    beat["source_utterance_ids"] = ["U1"]
    beat["clip_evidence"]["subjects"][0]["source_utterance_ids"] = ["U1"]
    beat["clip_evidence"]["speakers"][0]["source_utterance_ids"] = ["U1"]
    shard = tmp_path / "shard.json"
    shard.write_text(json.dumps(review))
    output = tmp_path / "combined.json"
    merge_scene_dialogue_review_shards(path, [shard], output)
    merged = json.loads(output.read_text())
    assert merged["scenes"][0]["editorial_beats"][0]["clip_evidence"]["subjects"][0][
        "source_utterance_ids"
    ] == ["G001-U1"]
    validate_scene_dialogue_review(packet, merged)


def test_person_observations_are_samples_not_absence_intervals():
    old = person_observations(
        {"visualIntervalSeconds": 1 / 3, "visualSamples": [{"faceCount": 0}]}, 10
    )
    assert old["status"] == "legacy_counts_only"
    assert old["coverage"] == "sampled_only"
    assert old["observations"] == []
    invalid = copy.deepcopy(old)
    invalid["observations"] = [
        {
            "observation_id": "x",
            "kind": "face",
            "timestamp": 2,
            "bounding_box": [0.9, 0, 0.3, 0.5],
            "confidence": 1,
        }
    ]
    with pytest.raises(ValueError, match="normalized"):
        validate_person_observations(invalid, 10)
    partial = person_observations(
        {
            "visualIntervalSeconds": 1 / 3,
            "faceObservations": [
                {
                    "timestamp": 2,
                    "boundingBox": [-0.1, 0.2, 0.4, 0.9],
                    "confidence": 0.8,
                }
            ],
            "personObservations": [],
        },
        10,
    )
    assert partial["observations"][0]["frame_edge_truncated"] is True
    assert partial["observations"][0]["bounding_box"] == pytest.approx(
        [0, 0.2, 0.3, 0.8]
    )


def test_legacy_candidates_remain_unknown(enriched, tmp_path):
    timeline, path, packet, review = enriched
    for scene in review["scenes"]:
        for beat in scene["editorial_beats"]:
            del beat["clip_evidence"]
    packet["policy"]["clip_evidence_required"] = False
    path.write_text(json.dumps(packet))
    review_path = tmp_path / "legacy.json"
    review_path.write_text(json.dumps(review))
    output = tmp_path / "merged.json"
    merge_scene_dialogue_review(timeline, path, review_path, output)
    records = build_candidate_library(json.loads(output.read_text()))["candidates"]
    assert all(
        c["evidence"] is None and c["readiness"] == "needs_review" for c in records
    )


def test_empty_global_beats_preserve_group_candidates(enriched, tmp_path):
    timeline, packet_path, _, review = enriched
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review))
    output = tmp_path / "merged.json"
    merge_scene_dialogue_review(timeline, packet_path, review_path, output)
    merged = json.loads(output.read_text())
    expected = build_candidate_library(merged)["candidates"]
    assert expected
    merged["reviewed_dialogue"]["editorial_beats"] = []
    assert build_candidate_library(merged)["candidates"] == expected
