# Architecture & internals

Implementation detail behind [screenstudio-alt](../README.md). The README is the human intro;
this is the deep end.

> **macOS-only, end to end.** Not just the logger: `render.py` encodes with Apple's
> `h264_videotoolbox`, `polish.py` loads `/System/Library/Fonts/Helvetica.ttc`, and capture
> relies on `screencapture` and the Swift `events-log` logger — all macOS. Not portable to
> Linux/Windows as written.

## Running by hand (CLI)

```bash
pip install -r requirements.txt            # numpy + Pillow
brew install ffmpeg
swiftc -O src/events-log.swift -o src/events-log   # one time

# record with any recorder while logging input:
./src/events-log demo.events.jsonl & LOGGER=$!
screencapture -v -V 30 demo.mov
kill $LOGGER

# polish:
python3 src/polish.py demo.mov --events demo.events.jsonl --speedup --zoom --keys --vertical
# → demo.polished.mov  +  demo.polished.vertical.mov (1080×1920)
```

`--speedup` alone works on **any** existing recording — no event log needed.
Prefer a GUI? `python3 src/studio.py demo.mov` opens a local timeline editor.

## How it works

Screen Studio's signature effects need to know where you clicked and what you typed — pixels
alone can't tell you that. So the tool has two halves:

1. **`events-log`** (Swift) runs *while you record* and logs cursor positions (60 Hz), clicks,
   and keystrokes to a JSONL file. It drops all key events while macOS secure input is active
   (password fields, sudo) and is meant to run only for the duration of a recording.
2. **`polish.py`** (Python) post-processes the recording using that log:

| Flag | Effect | Needs events? |
|---|---|---|
| `--speedup` | Compress idle spans (no input AND frozen pixels — a playing animation is never compressed) | no (pixel-only fallback) |
| `--zoom` | Eased auto-zoom onto click clusters | yes |
| `--keys` | Accumulating keystroke chips, rendered like real typing | yes |
| `--smooth-cursor` | Replace the jittery real cursor with an eased synthetic one (record cursor-free for best results) | yes |
| `--vertical` | Additional 1080×1920 output whose crop window follows the action | best with |

`numpy` is required: `render.py` — the renderer every export shells out to — uses it for
per-frame camera math, backdrop gradients, and click-sound mixing. `ffmpeg` is a system
dependency (Homebrew), not a pip package.

Pairs with [macos-screen-recorder-system-audio](https://github.com/connerkward/macos-screen-recorder-system-audio)
(`sck-record --no-cursor`) for system-audio capture and cursor-free footage for `--smooth-cursor`.

## High-quality renderer (`render.py`)

`render.py` is a **non-destructive, single-pass** camera renderer — the Screen Studio approach.
The recording is never modified; a render spec (zoom regions + ease ramp + fps + aspect) drives
a virtual camera that zooms/pans over the **original high-res frames**, sampled with **LANCZOS**,
exported to a **smaller target** — so a zoom still reads ≥1:1 source pixels and stays crisp
(measured ~1.3× sharper than cropping a finished video, more with native-retina capture). Easing
is **cosine ease-in/out** per region, tuned by `--ramp`, output at **60fps**, doing horizontal
(`16:9`/`1:1`) *and* 9:16 vertical (`--aspect 9:16`, zoom + follow) from the same code.

```bash
python3 src/render.py SRC.mp4 --regions regions.json --out out.mp4 \
  --aspect 16:9 --fps 60 --ramp 0.5 --cursor --speedup
# vertical: same command with --aspect 9:16
```

`regions.json` = `[{"t0","t1","z","cx","cy"}]`. (`polish.py` is the older ffmpeg-filter path —
it does the `--speedup`/idle-detection work and needs no numpy itself, but it is **not** a
substitute for `render.py`: every Studio export and high-quality zoom/pan goes through `render.py`.)

## Text callouts (`--callouts`)

Screen-fixed explanatory labels, drawn **last** on the final output frame (so they stay put
while the camera zooms/pans). `--callouts '[{"t0","t1","text","anchor","size"}]'` (OUTPUT time).
Each label is **background-aware** — it samples the luminance behind it and flips between a dark
pill / white text (over light content) and a light pill / dark text (over dark content), the same
contrast trick the click ripple uses — and **location-aware** via `anchor`
(`top-left` · `top` · `top-right` · `left` · `center` · `right` · `bottom-left` · `bottom` ·
`bottom-right`), keeping it at the edges, off the content. Fades in/out at the span edges.

```bash
python3 src/render.py demo.mp4 --regions regions.json --out out.mp4 \
  --callouts '[{"t0":0.5,"t1":4,"text":"auto-zoom detection","anchor":"top-right"}]'
```

Reuses the keystroke-chip rendering approach (PIL text on a rounded pill). A web view to place
callouts by eye and export the JSON is on the roadmap.

## Studio UI — single-clip editor (`studio.py`)

`python3 src/studio.py [recording.mp4]` opens a local web UI: an **NLE-style timeline with a fixed
ruler** (bar width = source duration; edits never rescale it, so upstream content is always
planted). Auto-detected **zoom** regions are draggable blocks — drag to move, drag edges to
retime, scroll to set zoom level, double-click to add/delete. Idle spans are auto-detected as the
**intersection of input-gaps and frozen pixels** and become **speed blocks**: their source range
is locked but their **rate is editable** — inspector slider, or **drag the right edge = rate-stretch**
(FCP retime / Premiere Rate Stretch). Rate changes ripple downstream along the planted ruler.
Configurable ease, default zoom, and frame styling (backdrop gradient + padding + rounded corners
+ drop shadow). Preview is live canvas compositing; export uses `render.py` at 60fps. Synthetic
cursor is always smooth; click ripple + a real recorded click sound (CC0, freesound #735771).
OS-chosen free port, all local.

Three competitor-parity additions: **GIF export** (two-pass palette transcode for README/social),
**style presets** (named look — aspect/fit/background/padding/radius/shadow/ramp/zoom/toggles —
persisted in `presets.json`), and **activity-aware auto-zoom** (fires on standalone typing bursts,
not only click clusters).

## Sequence UI — multi-clip editor (`sequence.py`)

`studio.py` edits **one** recording; `sequence.py` is the other half — a **multi-clip NLE**:
arrange clips end-to-end on one track, trim each, reorder by drag, export the concatenation.
Hard cuts only; no per-clip effects (that's `studio.py`'s job).

```bash
python3 src/sequence.py clip1.mp4 clip2.mov   # seed from CLI
python3 src/sequence.py /path/to/folder        # or a folder of videos
python3 src/sequence.py                        # or start empty, add in-browser
```

- **Trim** — drag a clip's edge (fixed px-per-second scale, frozen during drag, so the edge tracks 1:1).
- **Reorder** — drag a clip body; others slide to new slots, snap on drop.
- **Scrub / play** — ▶ / spacebar plays straight through; **double-buffered** playback (two `<video>`
  elements) makes crossing a cut an instant swap.
- **Export video** — concatenates trimmed clips to **1080p 60fps** mp4 (16:9 / 9:16 / 1:1 selector;
  differing clips scaled-to-fit + pillar/letterboxed; silent clips get synthesized silence). Encoder
  `h264_videotoolbox` with `libx264` fallback.
- **Beat-cut submode** — type a BPM, pick a segment length (½/1/2/4 beats), snap every clip to the
  beat grid for a music-synced reel. ⌘Z / ⌘⇧Z undo/redo.
- **Export GIF** and **Export FCPXML 1.9** (editable handoff into Resolve / Final Cut / Premiere).

Any pipeline that emits a folder of mp4s can hand them straight here (explicit order = timeline
order, or seed a folder alphabetically). Gotcha: thumbnails cache at `.studio-out/seqclips/<id>.jpg`
keyed by clip id — `rm -f .studio-out/seqclips/*.jpg` before relaunch when the clip set changes.

## Permissions

The logger needs **Accessibility / Input Monitoring** for clicks + keys (System Settings → Privacy
& Security); without the grant it degrades to cursor-move sampling. The recorder needs **Screen
Recording**. This is a demo-production tool: run the logger only while recording, and treat the
events file like the recording itself.

## Testing

`make-fixture.py` synthesizes a fake screen recording with scripted cursor travel, clicks, typing,
and idle spans, plus a ground-truth `events.jsonl` — every feature is validated against known truth:

```bash
python3 test/make-fixture.py test/fixture.mp4 test/fixture.events.jsonl
python3 src/polish.py test/fixture.mp4 --events test/fixture.events.jsonl --speedup --zoom --keys --vertical
```
