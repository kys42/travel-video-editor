# Travel Video Editor working context

Before reading or writing project media, read `docs/media-locations.md`.

- Treat `/Volumes/T7/시애틀알래스카` as immutable 4K/camera source media. Never rename, move, overwrite, or delete files there.
- Use `/Volumes/ExternalSSD/travel-video-editor` for regenerable working media. The active 1080p proxies are under `proxies/1080p-h264`.
- Keep every analysis and edit decision linked to the original relative path and source time range. Use proxies for processing and review, then relink final edits to the originals.
- Keep large media outside Git. Store code, manifests, structured metadata, and documentation in this repository according to the retention rules in the media-locations document.
