from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .apple_speech import AppleSTTConfig, process_apple_stt
from .context import build_context_packet, merge_context_review, validate_context_review
from .dialogue_script import attach_dialogue_script
from .library import render_video_library
from .phase1 import Phase1Config, process_assets
from .proxy import ProxyConfig, build_proxy_batch
from .review import load_json, merge_review, validate_review
from .scene_dialogue import (
    build_no_candidate_scene_dialogue_review,
    build_scene_dialogue_packet,
    merge_scene_dialogue_review,
    merge_scene_dialogue_review_shards,
    slice_scene_dialogue_packet,
    validate_boundary_proposals,
    validate_scene_dialogue_review,
)
from .speech import AdaptiveSTTConfig, process_adaptive_stt
from .transcript_reconcile import (
    build_reconciliation_packet,
    merge_reconciliation,
    render_reconciliation_html,
    validate_reconciliation_review,
)
from .transcript_attach import attach_reconciled_transcript
from .video_summary import (
    build_video_summary_packet,
    merge_video_summary,
    validate_video_summary,
)
from .web import render_timeline_web


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="travel-video")
    subparsers = parser.add_subparsers(dest="command", required=True)

    phase1 = subparsers.add_parser(
        "phase1", help="Build a machine-first scene timeline"
    )
    phase1.add_argument("sources", nargs="+", type=Path)
    phase1.add_argument("--output-root", type=Path, default=Path("work/phase1"))
    phase1.add_argument("--sample-interval", type=float, default=5.0)
    phase1.add_argument("--max-segment", type=float, default=30.0)
    phase1.add_argument("--min-segment", type=float, default=5.0)
    phase1.add_argument("--change-threshold", type=float, default=0.19)
    phase1.add_argument("--group-threshold", type=float, default=0.24)
    phase1.add_argument("--thumbnail-width", type=int, default=640)
    phase1.add_argument(
        "--hwaccel", choices=("auto", "off", "videotoolbox"), default="auto"
    )
    phase1.add_argument("--stt", choices=("off", "mlx"), default="off")
    phase1.add_argument("--stt-model", default="mlx-community/whisper-tiny")
    phase1.add_argument(
        "--stt-languages",
        default="ko,en",
        help="Comma-separated language candidates to transcribe and preserve",
    )

    adaptive_stt = subparsers.add_parser(
        "stt-adaptive",
        help="Run VAD-bounded language detection and routed MLX transcription",
    )
    adaptive_stt.add_argument("source", type=Path)
    adaptive_stt.add_argument("--output-dir", type=Path, required=True)
    adaptive_stt.add_argument("--model", default="mlx-community/whisper-small-mlx")
    adaptive_stt.add_argument("--expected-languages", default="ko,en")
    adaptive_stt.add_argument("--silence-db", type=int, default=-35)
    adaptive_stt.add_argument("--silence-duration", type=float, default=0.6)
    adaptive_stt.add_argument("--max-chunk", type=float, default=15.0)
    adaptive_stt.add_argument("--min-chunk", type=float, default=1.0)
    adaptive_stt.add_argument("--language-probability", type=float, default=0.80)
    adaptive_stt.add_argument("--language-margin", type=float, default=0.20)
    adaptive_stt.add_argument(
        "--no-uncertain-fallback",
        action="store_false",
        dest="fallback_on_uncertain",
        help="Do not generate alternate-language candidates for uncertain chunks",
    )

    apple_stt = subparsers.add_parser(
        "stt-apple",
        help="Run Apple SpeechDetector with locale-specific SpeechTranscribers",
    )
    apple_stt.add_argument("source", type=Path)
    apple_stt.add_argument("--output-dir", type=Path, required=True)
    apple_stt.add_argument(
        "--locales",
        default="ko-KR,en-US",
        help="Comma-separated Apple Speech locales; each raw result is preserved",
    )
    apple_stt.add_argument(
        "--detector-sensitivity",
        choices=("low", "medium", "high"),
        default="medium",
    )
    apple_stt.add_argument(
        "--swift-package",
        type=Path,
        help="Override the bundled apple-speech Swift package path",
    )

    reconciliation_packet = subparsers.add_parser(
        "build-transcript-reconciliation-packet",
        help="Align Apple locale candidates with optional MLX and scene evidence",
    )
    reconciliation_packet.add_argument("apple_transcript", type=Path)
    reconciliation_packet.add_argument("--output", type=Path, required=True)
    reconciliation_packet.add_argument("--mlx-normalized", type=Path)
    reconciliation_packet.add_argument("--timeline", type=Path)
    reconciliation_packet.add_argument("--max-window", type=float, default=6.0)

    validate_reconciliation = subparsers.add_parser(
        "validate-transcript-reconciliation",
        help="Validate original-language transcript reconciliation",
    )
    validate_reconciliation.add_argument("packet", type=Path)
    validate_reconciliation.add_argument("review", type=Path)

    merge_transcript = subparsers.add_parser(
        "merge-transcript-reconciliation",
        help="Merge a validated reconciliation while keeping translations separate",
    )
    merge_transcript.add_argument("packet", type=Path)
    merge_transcript.add_argument("review", type=Path)
    merge_transcript.add_argument("--output", type=Path, required=True)

    render_transcript = subparsers.add_parser(
        "render-transcript-reconciliation",
        help="Render side-by-side raw candidates and reconciled utterances",
    )
    render_transcript.add_argument("packet", type=Path)
    render_transcript.add_argument("--review", type=Path)
    render_transcript.add_argument("--output", type=Path, required=True)

    scene_dialogue_packet = subparsers.add_parser(
        "build-scene-dialogue-review-packet",
        help="Build fresh Apple STT windows grouped by scene for one-pass dialogue review",
    )
    scene_dialogue_packet.add_argument("apple_transcript", type=Path)
    scene_dialogue_packet.add_argument("timeline", type=Path)
    scene_dialogue_packet.add_argument("--output", type=Path, required=True)
    scene_dialogue_packet.add_argument("--mlx-normalized", type=Path)
    scene_dialogue_packet.add_argument("--max-window", type=float, default=8.0)
    scene_dialogue_packet.add_argument(
        "--visual-packet",
        type=Path,
        help=(
            "Use a fresh phase1 context packet for one-pass visual, dialogue, "
            "caption, and editorial-beat review"
        ),
    )
    scene_dialogue_packet.add_argument(
        "--boundary-proposals",
        type=Path,
        help=(
            "Add lineage-checked boundary-proposal/v1 evidence to the integrated "
            "visual/dialogue review"
        ),
    )

    validate_boundaries = subparsers.add_parser(
        "validate-boundary-proposals",
        help="Validate boundary-proposal/v1 lineage, timing, and evidence",
    )
    validate_boundaries.add_argument("proposals", type=Path)
    validate_boundaries.add_argument("timeline", type=Path)

    validate_scene_dialogue = subparsers.add_parser(
        "validate-scene-dialogue-review",
        help="Validate one-pass original utterances and caption-ready display lines",
    )
    validate_scene_dialogue.add_argument("packet", type=Path)
    validate_scene_dialogue.add_argument("review", type=Path)

    no_candidate_scene_dialogue = subparsers.add_parser(
        "build-no-candidate-scene-dialogue-review",
        help="Create a deterministic empty review for a packet with zero Apple windows",
    )
    no_candidate_scene_dialogue.add_argument("packet", type=Path)
    no_candidate_scene_dialogue.add_argument("--output", type=Path, required=True)

    slice_scene_dialogue = subparsers.add_parser(
        "slice-scene-dialogue-review-packet",
        help="Create a complete-scene packet slice for parallel review",
    )
    slice_scene_dialogue.add_argument("packet", type=Path)
    slice_scene_dialogue.add_argument(
        "--groups", required=True, help="Comma-separated context group IDs"
    )
    slice_scene_dialogue.add_argument("--output", type=Path, required=True)

    merge_scene_dialogue_shards = subparsers.add_parser(
        "merge-scene-dialogue-review-shards",
        help="Merge validated complete-scene review shards",
    )
    merge_scene_dialogue_shards.add_argument("packet", type=Path)
    merge_scene_dialogue_shards.add_argument("reviews", nargs="+", type=Path)
    merge_scene_dialogue_shards.add_argument("--output", type=Path, required=True)

    merge_scene_dialogue = subparsers.add_parser(
        "merge-scene-dialogue-review",
        help="Attach validated one-pass dialogue review to a new timeline artifact",
    )
    merge_scene_dialogue.add_argument("timeline", type=Path)
    merge_scene_dialogue.add_argument("packet", type=Path)
    merge_scene_dialogue.add_argument("review", type=Path)
    merge_scene_dialogue.add_argument("--output", type=Path, required=True)

    attach_transcript = subparsers.add_parser(
        "attach-reconciled-transcript",
        help="Attach a validated reconciled transcript to timeline scenes",
    )
    attach_transcript.add_argument("timeline", type=Path)
    attach_transcript.add_argument("transcript", type=Path)
    attach_transcript.add_argument("--output", type=Path, required=True)

    proxy_batch = subparsers.add_parser(
        "proxy-batch",
        help="Create verified, resumable 1080p H.264 proxies",
    )
    proxy_batch.add_argument("source_root", type=Path)
    proxy_batch.add_argument("--output-root", type=Path, required=True)
    proxy_batch.add_argument("--max-dimension", type=int, default=1920)
    proxy_batch.add_argument("--bitrate-kbps", type=int, default=6000)
    proxy_batch.add_argument("--high-fps-bitrate-kbps", type=int, default=8000)
    proxy_batch.add_argument("--limit", type=int)

    validate = subparsers.add_parser(
        "validate-review", help="Validate Codex/human review JSON"
    )
    validate.add_argument("machine", type=Path)
    validate.add_argument("review", type=Path)

    merge = subparsers.add_parser(
        "merge-review", help="Merge validated review into machine timeline"
    )
    merge.add_argument("machine", type=Path)
    merge.add_argument("review", type=Path)
    merge.add_argument("--output", type=Path, required=True)

    context = subparsers.add_parser(
        "build-context-packet", help="Build multi-frame storyboards for reviewed groups"
    )
    context.add_argument("reviewed", type=Path)
    context.add_argument("--output-dir", type=Path, required=True)
    context.add_argument("--max-frames", type=int, default=12)

    validate_context = subparsers.add_parser(
        "validate-context-review", help="Validate second-pass context review"
    )
    validate_context.add_argument("packet", type=Path)
    validate_context.add_argument("review", type=Path)

    merge_context = subparsers.add_parser(
        "merge-context-review", help="Merge storyboard review into final timeline"
    )
    merge_context.add_argument("reviewed", type=Path)
    merge_context.add_argument("packet", type=Path)
    merge_context.add_argument("review", type=Path)
    merge_context.add_argument("--output", type=Path, required=True)

    render_web = subparsers.add_parser(
        "render-web", help="Render an interactive, reusable scene timeline"
    )
    render_web.add_argument("timeline", type=Path)
    render_web.add_argument("--output-dir", type=Path, required=True)
    render_web.add_argument("--frame-limit", type=int, default=16)
    render_web.add_argument(
        "--assets",
        choices=("embed", "relative"),
        default="embed",
        help="Embed images for portable HTML or link existing frame files",
    )

    summary_packet = subparsers.add_parser(
        "build-video-summary-packet",
        help="Build a token-light packet for whole-video synthesis",
    )
    summary_packet.add_argument("timeline", type=Path)
    summary_packet.add_argument("--output", type=Path, required=True)

    validate_summary = subparsers.add_parser(
        "validate-video-summary", help="Validate whole-video summary JSON"
    )
    validate_summary.add_argument("packet", type=Path)
    validate_summary.add_argument("review", type=Path)

    merge_summary = subparsers.add_parser(
        "merge-video-summary", help="Merge a validated whole-video summary"
    )
    merge_summary.add_argument("timeline", type=Path)
    merge_summary.add_argument("packet", type=Path)
    merge_summary.add_argument("review", type=Path)
    merge_summary.add_argument("--output", type=Path, required=True)

    render_library = subparsers.add_parser(
        "render-library", help="Render summarized videos in capture-time order"
    )
    render_library.add_argument("timelines", nargs="+", type=Path)
    render_library.add_argument("--output-dir", type=Path, required=True)
    render_library.add_argument("--title", default="Travel video field log")
    render_library.add_argument(
        "--proxy-root",
        type=Path,
        help="Directory containing completed H.264 proxy videos",
    )
    render_library.add_argument(
        "--assets",
        choices=("embed", "relative"),
        default="embed",
        help="Embed representative images or link existing frame files",
    )

    dialogue_script = subparsers.add_parser(
        "build-dialogue-script",
        help="Group reconciled utterances into readable timed dialogue lines",
    )
    dialogue_script.add_argument("timeline", type=Path)
    dialogue_script.add_argument("--output", type=Path, required=True)
    dialogue_script.add_argument("--max-gap", type=float, default=2.2)
    dialogue_script.add_argument("--max-duration", type=float, default=20.0)
    dialogue_script.add_argument("--max-chars", type=int, default=160)

    serve = subparsers.add_parser(
        "serve-editor", help="Serve the Edit Desk and contract-first agent API"
    )
    serve.add_argument("manifest", type=Path, help="render-library manifest.json")
    serve.add_argument("--state-dir", type=Path, default=Path("work/editor-state"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--agent-backend", choices=("auto", "codex", "demo"), default="auto"
    )
    serve.add_argument("--codex-model", default="gpt-5.6-terra")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "phase1":
            stt_languages = tuple(
                item.strip() for item in args.stt_languages.split(",") if item.strip()
            )
            if args.stt == "mlx" and not stt_languages:
                raise ValueError(
                    "--stt-languages must contain at least one language when STT is enabled"
                )
            config = Phase1Config(
                sample_interval=args.sample_interval,
                max_segment=args.max_segment,
                min_segment=args.min_segment,
                change_threshold=args.change_threshold,
                group_threshold=args.group_threshold,
                thumbnail_width=args.thumbnail_width,
                hwaccel=args.hwaccel,
                stt=args.stt,
                stt_model=args.stt_model,
                stt_languages=stt_languages,
            )
            for output in process_assets(args.sources, args.output_root, config):
                print(output)
        elif args.command == "stt-adaptive":
            expected_languages = tuple(
                item.strip()
                for item in args.expected_languages.split(",")
                if item.strip()
            )
            config = AdaptiveSTTConfig(
                model=args.model,
                expected_languages=expected_languages,
                silence_db=args.silence_db,
                silence_duration=args.silence_duration,
                min_chunk=args.min_chunk,
                max_chunk=args.max_chunk,
                min_language_probability=args.language_probability,
                min_language_margin=args.language_margin,
                fallback_on_uncertain=args.fallback_on_uncertain,
            )
            print(process_adaptive_stt(args.source, args.output_dir, config))
        elif args.command == "stt-apple":
            locales = tuple(
                item.strip() for item in args.locales.split(",") if item.strip()
            )
            config = AppleSTTConfig(
                locales=locales,
                detector_sensitivity=args.detector_sensitivity,
                swift_package=args.swift_package,
            )
            print(process_apple_stt(args.source, args.output_dir, config))
        elif args.command == "build-transcript-reconciliation-packet":
            output = build_reconciliation_packet(
                args.apple_transcript,
                args.output,
                mlx_normalized_path=args.mlx_normalized,
                timeline_path=args.timeline,
                max_window=args.max_window,
            )
            print(output)
        elif args.command == "validate-transcript-reconciliation":
            packet = load_json(args.packet)
            review = load_json(args.review)
            validate_reconciliation_review(packet, review)
            print("transcript reconciliation valid")
        elif args.command == "merge-transcript-reconciliation":
            print(merge_reconciliation(args.packet, args.review, args.output))
        elif args.command == "render-transcript-reconciliation":
            print(
                render_reconciliation_html(
                    args.packet,
                    args.output,
                    review_path=args.review,
                )
            )
        elif args.command == "build-scene-dialogue-review-packet":
            print(
                build_scene_dialogue_packet(
                    args.apple_transcript,
                    args.timeline,
                    args.output,
                    mlx_normalized_path=args.mlx_normalized,
                    max_window=args.max_window,
                    visual_packet_path=args.visual_packet,
                    boundary_proposals_path=args.boundary_proposals,
                )
            )
        elif args.command == "validate-scene-dialogue-review":
            validate_scene_dialogue_review(
                load_json(args.packet),
                load_json(args.review),
            )
            print("scene dialogue review valid")
        elif args.command == "validate-boundary-proposals":
            validate_boundary_proposals(args.proposals, args.timeline)
            print("boundary proposals valid")
        elif args.command == "build-no-candidate-scene-dialogue-review":
            print(
                build_no_candidate_scene_dialogue_review(
                    args.packet,
                    args.output,
                )
            )
        elif args.command == "slice-scene-dialogue-review-packet":
            group_ids = [
                item.strip() for item in args.groups.split(",") if item.strip()
            ]
            print(slice_scene_dialogue_packet(args.packet, group_ids, args.output))
        elif args.command == "merge-scene-dialogue-review-shards":
            print(
                merge_scene_dialogue_review_shards(
                    args.packet,
                    args.reviews,
                    args.output,
                )
            )
        elif args.command == "merge-scene-dialogue-review":
            print(
                merge_scene_dialogue_review(
                    args.timeline,
                    args.packet,
                    args.review,
                    args.output,
                )
            )
        elif args.command == "attach-reconciled-transcript":
            print(
                attach_reconciled_transcript(
                    args.timeline,
                    args.transcript,
                    args.output,
                )
            )
        elif args.command == "proxy-batch":
            config = ProxyConfig(
                max_dimension=args.max_dimension,
                bitrate_kbps=args.bitrate_kbps,
                high_fps_bitrate_kbps=args.high_fps_bitrate_kbps,
            )
            print(
                build_proxy_batch(
                    args.source_root,
                    args.output_root,
                    config,
                    limit=args.limit,
                )
            )
        elif args.command == "validate-review":
            validate_review(load_json(args.machine), load_json(args.review))
            print("review valid")
        elif args.command == "merge-review":
            merge_review(args.machine, args.review, args.output)
            print(args.output)
        elif args.command == "build-context-packet":
            _, packet_path = build_context_packet(
                args.reviewed, args.output_dir, max_frames=args.max_frames
            )
            print(packet_path)
        elif args.command == "validate-context-review":
            validate_context_review(load_json(args.packet), load_json(args.review))
            print("context review valid")
        elif args.command == "merge-context-review":
            merge_context_review(args.reviewed, args.packet, args.review, args.output)
            print(args.output)
        elif args.command == "render-web":
            output = render_timeline_web(
                args.timeline,
                args.output_dir,
                frame_limit=args.frame_limit,
                asset_mode=args.assets,
            )
            print(output)
        elif args.command == "build-video-summary-packet":
            build_video_summary_packet(args.timeline, args.output)
            print(args.output)
        elif args.command == "validate-video-summary":
            validate_video_summary(load_json(args.packet), load_json(args.review))
            print("video summary valid")
        elif args.command == "merge-video-summary":
            merge_video_summary(
                args.timeline,
                args.packet,
                args.review,
                args.output,
            )
            print(args.output)
        elif args.command == "render-library":
            output = render_video_library(
                args.timelines,
                args.output_dir,
                title=args.title,
                asset_mode=args.assets,
                proxy_root=args.proxy_root,
            )
            print(output)
        elif args.command == "build-dialogue-script":
            print(
                attach_dialogue_script(
                    args.timeline,
                    args.output,
                    max_gap=args.max_gap,
                    max_duration=args.max_duration,
                    max_chars=args.max_chars,
                )
            )
        elif args.command == "serve-editor":
            from .editor.server import serve_editor

            serve_editor(
                args.manifest,
                args.state_dir,
                host=args.host,
                port=args.port,
                agent_backend=args.agent_backend,
                codex_model=args.codex_model,
            )
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts failures to exit codes.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
