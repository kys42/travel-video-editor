from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .phase1 import Phase1Config, process_assets
from .review import load_json, merge_review, validate_review


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="travel-video")
    subparsers = parser.add_subparsers(dest="command", required=True)

    phase1 = subparsers.add_parser("phase1", help="Build a machine-first scene timeline")
    phase1.add_argument("sources", nargs="+", type=Path)
    phase1.add_argument("--output-root", type=Path, default=Path("work/phase1"))
    phase1.add_argument("--sample-interval", type=float, default=5.0)
    phase1.add_argument("--max-segment", type=float, default=30.0)
    phase1.add_argument("--min-segment", type=float, default=5.0)
    phase1.add_argument("--change-threshold", type=float, default=0.19)
    phase1.add_argument("--group-threshold", type=float, default=0.24)
    phase1.add_argument("--thumbnail-width", type=int, default=640)
    phase1.add_argument("--hwaccel", choices=("auto", "off", "videotoolbox"), default="auto")
    phase1.add_argument("--stt", choices=("off", "mlx"), default="off")
    phase1.add_argument("--stt-model", default="mlx-community/whisper-tiny")
    phase1.add_argument("--language", default=None)

    validate = subparsers.add_parser("validate-review", help="Validate Codex/human review JSON")
    validate.add_argument("machine", type=Path)
    validate.add_argument("review", type=Path)

    merge = subparsers.add_parser("merge-review", help="Merge validated review into machine timeline")
    merge.add_argument("machine", type=Path)
    merge.add_argument("review", type=Path)
    merge.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "phase1":
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
                language=args.language,
            )
            for output in process_assets(args.sources, args.output_root, config):
                print(output)
        elif args.command == "validate-review":
            validate_review(load_json(args.machine), load_json(args.review))
            print("review valid")
        elif args.command == "merge-review":
            merge_review(args.machine, args.review, args.output)
            print(args.output)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
