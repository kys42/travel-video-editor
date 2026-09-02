from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .context import build_context_packet, merge_context_review, validate_context_review
from .phase1 import Phase1Config, process_assets
from .review import load_json, merge_review, validate_review
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
            )
            print(output)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts failures to exit codes.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
