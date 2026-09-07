# Travel Video Editor — Design System

## Direction

**Edit Desk** is a compact desktop review workspace modeled on professional media tools: persistent footage navigation, a source-time-centered scene list, and a restrained source monitor. The interface disappears into selection and inspection work.

The primary usage scene is a single editor reviewing many clips for long sessions on a desktop monitor. Dark graphite surfaces reduce glare around imagery; warm amber marks the active range; cyan is reserved for audio and transcript evidence.

## Layout

- Application chrome is 44px high.
- `Review Workspace` is the single shared surface for the 220–240px footage bin and flexible source-time timeline. Review and Rough Cut must render this exact same DOM and behavior rather than maintaining visual copies.
- Review mode places the shared workspace beside a 300–360px source monitor.
- Rough Cut mode places the same shared workspace beside a 320–340px revision panel and a 320–360px AI chat panel. Only the outer shell and companion tools change.
- The timeline is the primary reading surface and gets the remaining width inside the shared workspace.
- Scene summaries stay compact. Opening a scene adds exactly one nested detail layer beneath that row.
- Interpretation belongs in the expanded row; the right panel is only for selected visual evidence, source range, and media state.
- At narrow widths, panels stack without changing the underlying information order.

## Typography

- Use one Korean-capable workhorse UI stack: system sans, Apple SD Gothic Neo, Noto Sans KR, Segoe UI.
- UI body is 10–12px at desktop density; key scene titles are 11–15px.
- Use SFMono-Regular/Menlo/Consolas only for timecodes, durations, codecs, and source measurements.
- No display type, promotional headline scale, tracked decorative labels, or editorial serif.

## Color

- Background: `#111315`
- Primary panel: `#171a1d`
- Raised/tool panel: `#1d2125`
- Control/selected neutral: `#23282d`
- Border: `#30363c`; strong divider: `#46505a`
- Primary text: `#e8ebed`; secondary: `#b6bdc4`; muted: `#89929b`; low-emphasis text: `#808a93`
- Active source range and edit attention: amber `#f0ad4e`
- Audio/STT evidence: cyan `#62b9ca`
- Success/high-confidence: `#75b798`
- Error/unreliable state: `#df6a6a`

Color always communicates selection, evidence type, confidence, or an error. It is not decorative.

## Components and behavior

- Footage rows combine one 16:10 thumbnail, title, filename, capture time, and duration.
- A source-time ruler mirrors scene duration and updates selection.
- Scene rows expose time, representative frame, action interpretation, dialogue summary, notable state, and confidence before expansion.
- For reusable candidates, expanded scenes start with a compact clip ledger: frame, title/time, synopsis/dialogue, subject and review signals. Dialogue, observed-people and review-needed filters narrow the ledger while preserving the ruler's original time positions.
- Candidate detail opens inline beneath one selected row. Keep the frame small, share one transport/close bar, and show populated observation, people, audio and quality sections immediately. Use container width to arrange frame, copy and facts in two or three columns; do not add another disclosure for these facts.
- Full group interpretation and dialogue remain available separately from raw machine samples and contextual storyboards. Older scenes without reusable candidates retain their existing detailed view.
- Only one scene is open at once inside the active clip to preserve scan position.
- Selecting a revision clip must select its footage row, open the linked scene in the shared workspace, and keep the scene summary visible after scrolling.
- The source monitor follows the selected scene and truthfully reports `프록시 없음` until proxy media exists.
- Controls require hover, active, disabled, and visible keyboard-focus states. Motion is limited to 150–200ms state transitions and respects reduced motion.

## Avoid

- Landing-page heroes and promotional copy.
- Equal-size metric or feature cards.
- Decorative gradients, texture, glow, glass, and oversized rounding.
- Hiding raw evidence behind a separate page when it can expand in context.
- Using the monitor as a second long-form reading column.
