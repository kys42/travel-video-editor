# First-use onboarding and interaction

Use this on the first invocation, a new machine/project/source, or a changed goal. Skill installation copies instructions; it does not execute a setup hook. On an ordinary resume, read the existing local profile and ask only about missing or changed decisions. If the user already supplied source, purpose, options, and authorization, use those answers and proceed.

## 1. Establish the task, then inspect without changing anything

Start with one short question if scope is missing:

> 어떤 작업을 원하세요? ① 장면·대사 추출과 웹 정리 ② 추출부터 하이라이트 제작까지 ③ 기존 라이브러리로 영상 편집

Ask for the source folder or existing library and a separate working/output folder if unknown. Do not ask users to upload full camera footage to chat. Existing conversation paths and manifests take precedence. Never adopt paths, trip names, identities, or exclusion preferences from this repository's examples as defaults for another user.

Perform cheap discovery first: current project, OS/architecture, Python/runtime, FFmpeg/ffprobe, uv, source reachability, file count/size and free working space. List files only inside the user-designated source; prefer existing manifests. Do not start STT, model downloads, proxy batches, or paid LLM calls during inventory. File dates can be unreliable: preserve camera metadata, distinguish story day from timezone conversions, and ask about date scope only if it affects selection.

Run the bundled read-only checker (substitute actual paths; outputs remain local):

```sh
python3 <skill-dir>/scripts/check_environment.py \
  --project-root /path/to/travel-video-editor \
  --source-root /path/to/camera-originals \
  --working-root /path/to/working-storage \
  --speech-backend auto --output /path/to/local/preflight.json
```

`ok` covers the checks actually performed, not every future backend. Choose `--speech-backend apple` after Apple STT is selected to require installed assets, or `off` when transcription is not needed. The checker does not download anything. A missing CLI or failed version command is an unmet dependency, even if its executable path exists.

Here `auto` means capability inspection only, not backend selection or transcription routing. Reuse an existing backend choice; otherwise explain compatible options and record the selected backend before processing. This checker verifies Apple assets only, so MLX readiness requires its own runtime/model checks. On Windows/Linux, the bundled Apple and MLX speech paths are unavailable; do not promise native local STT through these paths. Use shell-appropriate Python commands and path syntax (for example, `py` on Windows).

## 2. Explain only the preparation this task needs

Show a compact table: **ready / needs installation / unsupported / choice needed**, with the next action for each unmet item. Avoid a long questionnaire about settings that can be determined locally.

| Selected task | Required preparation |
|---|---|
| Existing library → edit | FFmpeg/ffprobe, Python + Pillow, readable selected media, output space; no new STT model |
| New footage → scene web | Project runtime, FFmpeg/ffprobe, working proxies/evidence; a supported speech backend only when speech extraction is wanted |
| Apple speech | macOS 26+, compatible Swift toolchain and device, built worker, requested locale + detector assets actually installed |
| MLX alternative | Apple Silicon and compatible MLX runtime/model; inspect project adaptive-STT guide before installing |
| No compatible local speech backend | Offer an existing transcript or explicit visual-only extraction; do not label missing speech as silence or silently select a cloud service |
| Model reasoning | The chosen agent's usable authentication and limits; don't read/copy credentials or ask the user to paste keys into chat |

For ordinary missing packages, state exactly what will be installed, where, whether a model download is involved, and any known disk/network need. If the user already asked to set up/install dependencies, do the authorized setup without re-asking. If they requested only inspection, present the concrete install plan and ask once before modifying the environment. OS upgrades, privileged prompts, account sign-in, or unknown installation size are distinct from a routine package install; let the user handle required system UI and don't claim completion before rechecking.

Prefer an existing package manager. Typical project setup is `uv sync --group dev` (or `uv sync` for runtime only). On a Mac with Homebrew already installed, missing FFmpeg/uv can be installed with `brew install ffmpeg uv`. Don't install a package manager or upgrade the OS as an unannounced side effect. Check current official installation instructions when no suitable package manager exists.

### Apple speech: check → optionally install → recheck

Apple manages these shared on-device assets. macOS version and a built binary alone do not prove model readiness. Check the selected locales and SpeechDetector configuration used by this project's transcriber.

```sh
swift build --package-path apple-speech -c release
apple-speech/.build/release/apple-speech capabilities

# Read-only: no audio is analyzed and nothing is downloaded.
apple-speech/.build/release/apple-speech assets --locale ko-KR
apple-speech/.build/release/apple-speech assets --locale en-US

# Only after installation is authorized; install the requested languages only.
apple-speech/.build/release/apple-speech assets --locale ko-KR --install
apple-speech/.build/release/apple-speech assets --locale en-US --install
```

The command reports `status_before`, `status_after`, and `installation_requested`. Only `installed` is ready. `supported`, `downloading`, `unsupported`, timeout, or an old worker without the `assets` command are not readiness. Rebuild an old worker; don't repeatedly start transcription to make model setup happen indirectly. Retry a failed install once if a transient cause is resolved, then report the actual blocker. Never remove other apps' locale reservations or models to free space automatically.

When the assets command is not present in the user's checkout, inspect `capabilities` and explain that fine-grained detector readiness/install needs the updated worker; do not invent a command. The existing `transcribe` path can download assets implicitly, so it is not a read-only setup test.

Source: [Apple AssetInventory](https://developer.apple.com/documentation/speech/assetinventory), which downloads and manages assets for configured analyzer modules. MLX and Apple assets are different backends; installing one does not prepare the other.

## 3. Ask a small brief, only as needed

Batch two or three related decisions per message and offer reasonable defaults. Explain the effect of a choice in ordinary language. Keep required unanswered choices pending; elapsed time is not consent.

For **extraction only**, gather date/source scope, spoken languages, desired outputs (scene web + reusable candidate JSON by default), and whether any material should be excluded. Do not ask aspect ratio, music, or film duration. Stop after the requested library deliverables.

For **highlight production**, gather these in stages:

1. **Purpose/audience and subject:** personal memory, family sharing, public travel vlog, short social clip; scenery, food, people/dialogue, activities, or a balanced story.
2. **Length and presentation:** desired runtime or range, horizontal/vertical, narrative chronology vs themed montage, calm vs brisk pacing. Defaults may be suggested, never silently treated as preferences.
3. **Voice and privacy:** keep conversations/original ambience, subtitle language and style, user-supplied music, faces/names/private conversations to exclude, sensitive scenes. Ask whom/what to exclude if ambiguous; anonymous person metadata is not identity or face-absence proof.
4. **Deliverables:** proxy preview first, final resolution/source relink, MP4/SRT/edit-plan outputs, destination. Public-facing purpose does not authorize uploading footage, making the GitHub repo public, or publishing the final film.

Example concise questions:

> 누구에게 보여줄 영상이고, 어떤 순간을 중심으로 남기고 싶으세요? 개인 기록 / 가족 공유 / 공개용 여행 영상

> 길이와 화면은 어떻게 할까요? 3분 가로 / 60초 세로 / 10분 이상 여행 기록

> 대사·자막·음악, 그리고 제외할 얼굴이나 장면 조건이 있나요?

Use the user's existing answers instead of replaying these examples verbatim.

## 4. Save the brief locally and proceed

Store accepted choices and environment findings in `<project>/work/onboarding/profile.json` or the user-selected local work directory. It is private runtime state, not a Git-tracked example. A minimal shape is:

```json
{
  "schema_version": "travel-video-onboarding/v1",
  "goal": "extract_and_highlight",
  "source_root": "/path/to/source",
  "working_root": "/path/to/work",
  "date_scope": [],
  "speech": {"backend": "apple", "locales": ["ko-KR", "en-US"]},
  "brief": {"purpose": "family", "subjects": ["scenery", "food"], "target_seconds": 180,
    "aspect_ratio": "16:9", "pace": "calm", "dialogue": "preserve", "captions": "original_language",
    "music_paths": [], "exclusions": [], "preview_first": true, "final_resolution": null},
  "setup": {"preflight_report": "preflight.json", "verified_at": null},
  "pending_questions": []
}
```

Use `null`/pending for unknowns and retain their meaning. Save no secrets, credentials, identity photos, or conversation transcript. This profile records choices, not a permanent blanket authorization. On resume, verify paths/current dependency readiness; don't redownload models or reprocess completed assets just because a new chat started.

Reflect the agreed task in one short paragraph with the source scope, outputs, edit constraints and any remaining dependency. If enough is already authorized, start; don't add another approval gate. When needed, do a small representative pilot before a large batch, reuse caches, and follow the current runbook's parallel extraction/automatic validation policy. Parent model reinspection of every asset is not an onboarding requirement.

For `extract_and_highlight`, finish the validated library and pass this same brief plus candidate paths to `video-editor`. Do not ask the questions again at the skill boundary. Before expensive rendering, show the concrete edit plan/preview according to the user's existing authorization. Report the actual stage, pending exceptions, output paths, and next action at interruption.
