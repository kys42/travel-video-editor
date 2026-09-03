from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def detect(manifest: dict[str, Any], model_name: str) -> None:
    import mlx.core as mx
    from mlx_whisper.audio import N_FRAMES, log_mel_spectrogram, pad_or_trim
    from mlx_whisper.transcribe import ModelHolder

    dtype = mx.float16
    model = ModelHolder.get_model(model_name, dtype)
    for job in manifest["jobs"]:
        output = Path(job["output_path"])
        if output.exists():
            continue
        mel = log_mel_spectrogram(job["audio_path"], n_mels=model.dims.n_mels)
        mel_segment = pad_or_trim(mel, N_FRAMES, axis=-2).astype(dtype)
        _, probabilities = model.detect_language(mel_segment)
        ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
        write_json(
            output,
            {
                "schema_version": "mlx-language-detection/v1",
                "chunk_id": job["chunk_id"],
                "audio_path": job["audio_path"],
                "model": model_name,
                "detected_language": ranked[0][0],
                "language_probabilities": probabilities,
            },
        )
        print(
            f"{job['chunk_id']}: detected {ranked[0][0]} "
            f"({ranked[0][1]:.3f}, margin {ranked[0][1] - ranked[1][1]:.3f})",
            flush=True,
        )


def transcribe(manifest: dict[str, Any], model_name: str) -> None:
    from mlx_whisper import transcribe as mlx_transcribe

    for route in manifest["routes"]:
        for language, output_path in route["transcript_paths"].items():
            output = Path(output_path)
            if output.exists():
                continue
            result = mlx_transcribe(
                route["audio_path"],
                path_or_hf_repo=model_name,
                language=language,
                task="transcribe",
                verbose=None,
                condition_on_previous_text=False,
            )
            result["routing"] = {
                "chunk_id": route["chunk_id"],
                "selected_language": route["selected_language"],
                "candidate_language": language,
                "uncertain": route["uncertain"],
                "uncertainty_reasons": route["uncertainty_reasons"],
            }
            write_json(output, result)
            print(f"{route['chunk_id']}: transcribed {language}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("detect", "transcribe"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.mode == "detect":
        detect(manifest, args.model)
    else:
        transcribe(manifest, args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
