# Editing onboarding

Start here when the user is new or their source/brief is incomplete. Existing user instructions, an edit plan, or `work/onboarding/profile.json` take precedence. Ask only missing decisions, not every question each time. Installing a skill does not execute setup; first invocation is the entry point.

## Choose the route

- **Explicit source ranges or an existing edit plan:** check render readiness and execute the requested edit. No new source analysis or speech model is needed just to trim, caption, or render.
- **Reviewed library/candidate JSON:** reuse it. Carry purpose and options from the extraction brief, then select/reorder cuts with traceable ranges.
- **Unanalyzed travel folder:** ask whether the user wants scene extraction only or extraction through final highlight. When the travel-video-pipeline skill and repository are available, use its `references/onboarding.md` for setup and extraction. Otherwise locate that project or proceed only with the narrower explicit editing task this skill can perform; do not pretend a missing extraction tool exists.

## Check render readiness

Inspect `ffmpeg -version`, `ffprobe -version`, Python and Pillow availability, readable source/proxy paths, installed caption fonts, and free output space. Use the existing project environment where present. The renderer's `--help` confirms it is runnable; `--dry-run` with an actual plan validates mappings and options. Do not create an invented plan to claim the user's footage is ready.

No Swift, Apple speech model, or MLX install is necessary for a render using reviewed data. Missing packages can be installed with an available package manager or project dependency setup when installation is authorized. For an inspection-only request, first show the specific packages, location and download implications; then ask once. Preserve existing environments, templates and media. Recheck after installation, and report unsupported OS/codec/font constraints instead of silently changing the output format.

## Gather a brief in two or three short rounds

1. **Purpose and story:** audience (private, family, public), subject emphasis (scenery, food, dialogue, activities), source/library and date scope.
2. **Shape:** desired length, 16:9/9:16 or another requested format, chronology/theme, calm/brisk pacing, required scenes and omissions.
3. **Sound and privacy:** original dialogue/ambience, subtitle language/style, available music, excluded people/faces/names/private dialogue, and output destination/resolution.

Offer meaningful alternatives when useful, and suggest defaults only for genuinely unspecified optional details. Do not infer that every user wants this repository owner's faceless travel montage or three-minute duration. Face exclusions need the selected imagery checked; anonymous person labels alone do not identify someone. A public-facing film brief does not authorize publishing media or changing repository visibility.

Examples:

> 개인 기록, 가족 공유, 공개용 영상 중 어떤 용도인가요? 어떤 장면을 가장 살리고 싶으세요?

> 원하는 길이와 화면은요? 짧은 세로 영상 / 3분 가로 하이라이트 / 긴 여행 기록

> 대사와 현장음을 살릴까요? 자막·음악·제외할 얼굴이나 장면 조건도 알려주세요.

Use free-form answers and don't force these presets. Keep unanswered essential choices pending. Ask no film-style questions for extraction-only requests.

## Carry choices into work

Save a small local editing brief next to the plan or reuse `work/onboarding/profile.json`; never commit real source paths, personal exclusions or credentials as a reusable example. Explain the concrete cut plan and make the requested preview, using already-granted execution authorization. A specific authorized trim/reorder/render does not require a new onboarding approval. If the user asked to review the plan first, respect that boundary.

Resolve renderer commands from this skill's actual installed directory, not a hardcoded agent home. If no filename was specified, choose a descriptive new file under the agreed output directory. On a collision, choose a new revision filename and report it; ask only when an exact destination or replacement is required. Do not enable overwrite implicitly.

For final source-relinked output, verify the selected originals are accessible with matching lineage/time mapping. If they are unavailable, offer a clearly labeled proxy output or wait for the source; never call a proxy upscale a source-quality 4K render. Return MP4, caption outputs where applicable, plan and render manifest. Preserve previous revisions and don't upload the result unless requested.
