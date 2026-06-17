#!/usr/bin/env python3
"""fcpxml.py — non-destructive "hand off to an NLE" exporter for the
screen-studio-alternative edit spec.

Instead of RENDERING pixels (what render.py does), this emits an **FCPXML 1.9**
document that opens in DaVinci Resolve / Final Cut Pro / Premiere as an editable
timeline laid over the UNTOUCHED source video. The zoom-and-speed edit is
reproduced with native NLE primitives (transform keyframes + retime), so the
human gets a real, editable project — no re-encode, no quality loss, source
never copied.

WHY FCPXML (and not Resolve scripting / OTIO)
---------------------------------------------
- Resolve's Python API has no keyframe-authoring surface and DaVinciResolveScript
  is Studio-only — you cannot script a zoom move into Resolve.
- OpenTimelineIO has no portable transform/effect schema; a zoom (scale+position
  keyframes) does not survive a round-trip through OTIO into Resolve.
- FCPXML carries `adjust-transform` scale/position `<keyframeAnimation>` (the
  zoom) AND retime via `<timeMap>` (the speed segments), and imports into
  Resolve, FCP, and Premiere. It is the only portable target for this edit.

THE EDIT SPEC (identical fields render.py consumes)
---------------------------------------------------
- src video path, width W, height H, fps.
- zoom regions: [{t0,t1,z,cx,cy}] — a cosine-eased virtual camera that scales by
  `z` centered on SOURCE-PIXEL (cx,cy); cosine ease in/out over `ramp` seconds at
  each end, holding `z` in between. z==1 is "no zoom" (rest).
- speed segments: [{t0,t1,speed}] — source spans [t0,t1] played `speed`x faster;
  the gaps between them play at 1x. Output (timeline) duration of a segment is
  (t1-t0)/speed. This is exactly render.py's `time_maps` integral.

HOW EACH SPEC FIELD MAPS TO FCPXML
----------------------------------
  src / W / H / fps      -> one <asset> + matching <format>; sequence <format>
                            == asset format (same WxH, same fps timebase).
  speed segment          -> the spine is SPLIT into one <asset-clip> per
                            contiguous source span (sped + 1x gaps). Each clip's
                            timeline `duration` is the retimed length; a
                            <timeMap> inside the clip maps clip-local time to the
                            source span so Resolve/FCP apply the speed change.
                            (Split-per-segment imports far more reliably than one
                            whole-clip multi-point timeMap.)
  zoom region            -> per overlapping clip, an <adjust-transform> with
                            `scale` and `position` <keyframeAnimation>. The cosine
                            ease is BAKED: sampled as N keyframes (~1 key / 2-4
                            frames across each ramp) so FCP's interpolation mode
                            is irrelevant and the motion matches render.py exactly.
  t0,t1 (region, SOURCE) -> remapped to OUTPUT (timeline) time through the speed
                            map (render.py does the same via s2o) before baking.
  ripple                 -> speed segments shorten total output; spine length and
                            sequence duration use the retimed integral, not the
                            raw source duration.

COORDINATE MAPPING — THE KNOWN FCPXML->RESOLVE QUIRK (READ THIS)
---------------------------------------------------------------
cx,cy are SOURCE PIXELS (origin top-left). FCPXML `position` is NORMALIZED and
CENTER-origin, and Resolve then RESCALES on import by its own (undocumented)
convention. The exact constant relating "source pixels" to "Resolve position
units after a `scale`" CANNOT be derived from the published FCPXML docs — it
depends on how the importing NLE composes scale-about-center with position.

This module implements the GEOMETRY:

    position_x = ((W/2 - cx) / W) * CALIBRATION
    position_y = ((cy - H/2) / H) * CALIBRATION

with CALIBRATION defaulting to 1.0. **This is NOT claimed pixel-accurate.** The
sign/axis convention is right (move the camera toward (cx,cy) as it zooms in),
but the magnitude must be CALIBRATED empirically — see the TODO below. The zoom
SCALE (z) is exact; only the pan MAGNITUDE needs calibration.

TODO(calibration): export a known move (e.g. z=2 centered on a marked corner) to
FCPXML, import into Resolve, and visually check where the zoom lands. Adjust
CALIBRATION until the on-screen center matches the marked source pixel. The
value likely differs between Resolve / FCP / Premiere; capture one per target.

API
---
  to_fcpxml(spec_dict) -> str
  CLI: python3 fcpxml.py SRC --regions regions.json --speed-segments segs.json \
                         --fps 60 --width 1920 --height 1080 --out edit.fcpxml
"""
import argparse
import json
import math
import os
import sys
from xml.sax.saxutils import escape, quoteattr

# --- Calibration constant for the FCPXML->Resolve pan-magnitude quirk. --------
# 1.0 = raw geometry. MUST be calibrated per target NLE (see module docstring).
# The zoom scale is exact; this only scales the pan (position) magnitude.
CALIBRATION = 1.0

# Keyframe density across a ramp: one sampled key every KEY_EVERY_FRAMES frames.
# ~1 key / 2-4 frames makes the baked cosine indistinguishable from render.py's
# per-frame ease while keeping the document small.
KEY_EVERY_FRAMES = 3


# ----------------------------------------------------------------------------
# Time / rational helpers. FCPXML times are exact rationals "N/Ds" on the fps
# timebase. Every time we emit is rounded to a frame boundary first.
# ----------------------------------------------------------------------------
def _rational(seconds, fps):
    """Round `seconds` to the nearest frame and return an exact FCPXML time
    string "frames*1/fps s" -> "<frames>/<fps>s" (or "0s")."""
    frames = int(round(seconds * fps))
    if frames == 0:
        return "0s"
    # FCPXML wants numerator/denominator with the timebase as denominator.
    return "%d/%ds" % (frames, fps)


def _frames(seconds, fps):
    return int(round(seconds * fps))


# ----------------------------------------------------------------------------
# Build the contiguous speed map covering the whole source timeline, exactly
# like render.py's time_maps(): every source second belongs to exactly one
# segment; gaps between provided speed segments are 1x.
# Returns:
#   segs:  [(s0, s1, speed, o0)]  contiguous, sorted, o0 = output start
#   out_dur: total retimed timeline duration
#   s2o(src_t) -> output_t  (for remapping region times)
# ----------------------------------------------------------------------------
def build_speed_map(speed_segments, src_dur):
    raw = sorted(
        ((float(s["t0"]), float(s["t1"]), float(s.get("speed", 1.0)))
         for s in speed_segments),
        key=lambda s: s[0],
    )
    # Validate non-overlap; fill gaps (and head/tail) with 1x spans.
    segs_src = []
    cursor = 0.0
    for s0, s1, sp in raw:
        if s1 <= s0:
            raise ValueError("speed segment t1 (%g) must be > t0 (%g)" % (s1, s0))
        if s0 < cursor - 1e-9:
            raise ValueError("overlapping speed segments near t=%g" % s0)
        if s0 > cursor + 1e-9:
            segs_src.append((cursor, s0, 1.0))   # 1x gap
        segs_src.append((s0, s1, sp))
        cursor = s1
    if src_dur > cursor + 1e-9:
        segs_src.append((cursor, src_dur, 1.0))  # 1x tail
    if not segs_src:                              # no speed segments at all
        segs_src = [(0.0, src_dur, 1.0)]

    segs, out = [], 0.0
    for s0, s1, sp in segs_src:
        segs.append((s0, s1, sp, out))
        out += (s1 - s0) / sp
    out_dur = out

    def s2o(st):
        for s0, s1, sp, o0 in segs:
            if st <= s1 or (s0, s1, sp, o0) == segs[-1]:
                clamped = min(max(st, s0), s1)
                return o0 + (clamped - s0) / sp
        return out_dur

    return segs, out_dur, s2o


# ----------------------------------------------------------------------------
# Cosine ease of one scalar over the OUTPUT timeline, matching render.py's
# ease_traj exactly: rest -> value over `ramp`, hold, ease back. Evaluated at an
# arbitrary output time `ot`.
# regions_o: [{o0,o1,val}] (output-time spans), rest: baseline value.
# ----------------------------------------------------------------------------
def _ease_value(regions_o, rest, ramp, ot):
    val = rest
    for r in regions_o:
        o0, o1, v = r["o0"], r["o1"], r["val"]
        if ot < o0 or ot > o1:
            continue
        rmp = min(ramp, (o1 - o0) / 2.0)
        if rmp > 0 and ot < o0 + rmp:
            a = (ot - o0) / rmp
            f = 0.5 - 0.5 * math.cos(math.pi * a)
        elif rmp > 0 and ot > o1 - rmp:
            a = (o1 - ot) / rmp
            f = 0.5 - 0.5 * math.cos(math.pi * a)
        else:
            f = 1.0
        val = rest + (v - rest) * f
    return val


def _position_for_center(cx, cy, W, H):
    """Geometry only (see module docstring's calibration caveat).
    Normalized, center-origin position that pans the camera toward (cx,cy).
    NOT claimed pixel-accurate; magnitude is governed by CALIBRATION."""
    px = ((W / 2.0 - cx) / W) * CALIBRATION
    py = ((cy - H / 2.0) / H) * CALIBRATION
    return px, py


# ----------------------------------------------------------------------------
# Keyframe sampling: collect the output-timeline times at which we need a
# scale/position key for a region, intersected with a clip's output span.
# We sample densely across each ramp (KEY_EVERY_FRAMES) and place explicit keys
# at the hold boundaries, all snapped to frame boundaries.
# ----------------------------------------------------------------------------
def _sample_times(region_o, clip_o0, clip_o1, ramp, fps):
    o0, o1 = region_o["o0"], region_o["o1"]
    rmp = min(ramp, (o1 - o0) / 2.0)
    times = set()

    def add(t):
        if clip_o0 - 1e-9 <= t <= clip_o1 + 1e-9:
            times.add(_frames(t, fps))

    # Rest keys just outside the region (so the move starts/ends at rest).
    add(o0)
    add(o1)
    # Hold boundaries.
    add(o0 + rmp)
    add(o1 - rmp)
    # Dense ramp-in samples.
    step = KEY_EVERY_FRAMES / float(fps)
    t = o0
    while t < o0 + rmp:
        add(t)
        t += step
    # Dense ramp-out samples.
    t = o1 - rmp
    while t < o1:
        add(t)
        t += step
    return sorted(times)


# ----------------------------------------------------------------------------
# Main builder.
# ----------------------------------------------------------------------------
def to_fcpxml(spec):
    """spec: {
        src: str (path), width: int, height: int, fps: number,
        duration: number (source seconds; optional, inferred from spec if absent),
        regions: [{t0,t1,z,cx,cy}],
        speed_segments: [{t0,t1,speed}],
        ramp: number (seconds, default 0.5),
        name: str (optional sequence/event name),
       }
    Returns an FCPXML 1.9 document string.
    """
    src = spec["src"]
    W = int(spec["width"])
    H = int(spec["height"])
    fps = float(spec["fps"])
    ramp = float(spec.get("ramp", 0.5))
    regions = spec.get("regions", []) or []
    speed_segments = spec.get("speed_segments", []) or []
    name = spec.get("name") or os.path.splitext(os.path.basename(src))[0]

    # Source duration: explicit, else the max time touched by any spec field.
    src_dur = spec.get("duration")
    if src_dur is None:
        ends = [0.0]
        ends += [float(r["t1"]) for r in regions]
        ends += [float(s["t1"]) for s in speed_segments]
        src_dur = max(ends)
    src_dur = float(src_dur)
    if src_dur <= 0:
        raise ValueError("source duration must be > 0 (got %g)" % src_dur)

    segs, out_dur, s2o = build_speed_map(speed_segments, src_dur)

    # Remap region times into OUTPUT timeline (render.py does this via s2o).
    regions_z = []
    regions_cx = []
    regions_cy = []
    for r in regions:
        o0 = s2o(float(r["t0"]))
        o1 = s2o(float(r["t1"]))
        if o1 <= o0:
            continue
        regions_z.append({"o0": o0, "o1": o1, "val": float(r.get("z", 2.0))})
        regions_cx.append({"o0": o0, "o1": o1, "val": float(r.get("cx", W / 2.0))})
        regions_cy.append({"o0": o0, "o1": o1, "val": float(r.get("cy", H / 2.0))})

    # FCPXML frame-duration string: "1/fps s" but fps may be fractional. We use
    # an integer timebase; for non-integer fps (e.g. 29.97) round the timebase.
    timebase = int(round(fps))
    frame_dur = "1/%ds" % timebase

    # Chain clip durations in integer frames so the spine is EXACTLY contiguous.
    # Independently rounding each clip's float output start (`_rational(o0)`) can
    # round offset[k+1] and offset[k]+duration[k] to frames that differ by 1 —
    # leaving a 1-frame gap (black) or overlap between adjacent clips on the
    # spine. Deriving every offset from one running integer-frame accumulator
    # removes that. (_self_test asserts the spine has no gap/overlap.)
    clip_dur_fr = [_frames((s1 - s0) / sp, fps) for (s0, s1, sp, o0) in segs]
    seq_dur_frames = sum(clip_dur_fr)

    def _fr_str(fr):
        return "0s" if fr == 0 else "%d/%ds" % (fr, timebase)

    fmt_id = "r1"
    asset_id = "r2"
    abs_src = os.path.abspath(os.path.expanduser(src))
    src_url = "file://" + abs_src

    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<!DOCTYPE fcpxml>')
    out.append('<fcpxml version="1.9">')
    out.append('  <resources>')
    out.append(
        '    <format id="%s" name="FFVideoFormat%dp%d" '
        'frameDuration="%s" width="%d" height="%d" '
        'colorSpace="1-1-1 (Rec. 709)"/>'
        % (fmt_id, H, timebase, frame_dur, W, H)
    )
    # The asset duration is the FULL source length; segments reference sub-spans.
    asset_dur = _rational(src_dur, fps)
    out.append(
        '    <asset id="%s" name=%s start="0s" duration="%s" '
        'hasVideo="1" format="%s" videoSources="1">'
        % (asset_id, quoteattr(name), asset_dur, fmt_id)
    )
    out.append(
        '      <media-rep kind="original-media" src=%s/>' % quoteattr(src_url)
    )
    out.append('    </asset>')
    out.append('  </resources>')
    out.append('  <library>')
    out.append('    <event name=%s>' % quoteattr(name + " (handoff)"))
    out.append('      <project name=%s>' % quoteattr(name))
    out.append(
        '        <sequence format="%s" duration="%s" '
        'tcStart="0s" tcFormat="NDF">'
        % (fmt_id, _fr_str(seq_dur_frames))
    )
    out.append('          <spine>')

    # One asset-clip per contiguous source span (segs). offset = output start,
    # chained in integer frames (off_fr) so the spine has no gap/overlap.
    off_fr = 0
    for idx, (s0, s1, sp, o0) in enumerate(segs):
        seg_src_dur = s1 - s0
        seg_out_dur = seg_src_dur / sp
        dur_fr = clip_dur_fr[idx]
        offset = _fr_str(off_fr)
        clip_dur = _fr_str(dur_fr)
        clip_start = _rational(s0, fps)          # where in the asset this span begins
        # For a sped clip, the asset span we consume is longer than the clip's
        # timeline duration; <timeMap> expresses that ratio.
        out.append(
            '            <asset-clip ref="%s" offset="%s" '
            'name=%s start="%s" duration="%s" format="%s" tcFormat="NDF">'
            % (asset_id, offset, quoteattr(name), clip_start, clip_dur, fmt_id)
        )

        # --- Retime (speed) -------------------------------------------------
        # timeMap maps clip-local timeline time -> source(asset) time. A 2x clip
        # maps [0 -> 0] and [clip_dur -> 2*clip_dur of source]. Two keyframes
        # give a constant-rate retime that round-trips into Resolve/FCP reliably
        # (a single straight-line segment; no easing on the speed itself).
        if abs(sp - 1.0) > 1e-9:
            out.append('              <timeMap>')
            out.append(
                '                <timept time="0s" value="0s" interp="linear"/>'
            )
            out.append(
                '                <timept time="%s" value="%s" interp="linear"/>'
                % (_rational(seg_out_dur, fps), _rational(seg_src_dur, fps))
            )
            out.append('              </timeMap>')

        # --- Zoom (adjust-transform with baked cosine keyframes) ------------
        clip_o0, clip_o1 = o0, o0 + seg_out_dur
        # Which regions touch this clip's output span?
        touching = [
            (rz, rcx, rcy)
            for rz, rcx, rcy in zip(regions_z, regions_cx, regions_cy)
            if rz["o1"] > clip_o0 + 1e-9 and rz["o0"] < clip_o1 - 1e-9
        ]
        if touching:
            # Union of sample times across all touching regions, snapped to frame.
            scale_keys = {}    # frame -> scale
            pos_keys = {}      # frame -> (x, y)
            for rz, rcx, rcy in touching:
                for fr in _sample_times(rz, clip_o0, clip_o1, ramp, fps):
                    ot = fr / fps
                    scale_keys[fr] = _ease_value(regions_z, 1.0, ramp, ot)
                    cx = _ease_value(regions_cx, W / 2.0, ramp, ot)
                    cy = _ease_value(regions_cy, H / 2.0, ramp, ot)
                    pos_keys[fr] = _position_for_center(cx, cy, W, H)

            if scale_keys:
                out.append('              <adjust-transform>')
                # scale keyframes (uniform x==y == z)
                out.append('                <param name="scale">')
                out.append('                  <keyframeAnimation>')
                for fr in sorted(scale_keys):
                    # keyframe time is CLIP-LOCAL: output time minus clip start.
                    local = _fr_str(fr - off_fr)   # clip-local frames (off_fr = rounded clip start)
                    z = scale_keys[fr]
                    out.append(
                        '                    <keyframe time="%s" '
                        'value="%.6f %.6f"/>' % (local, z, z)
                    )
                out.append('                  </keyframeAnimation>')
                out.append('                </param>')
                # position keyframes
                out.append('                <param name="position">')
                out.append('                  <keyframeAnimation>')
                for fr in sorted(pos_keys):
                    local = _fr_str(fr - off_fr)   # clip-local frames (off_fr = rounded clip start)
                    x, y = pos_keys[fr]
                    out.append(
                        '                    <keyframe time="%s" '
                        'value="%.6f %.6f"/>' % (local, x, y)
                    )
                out.append('                  </keyframeAnimation>')
                out.append('                </param>')
                out.append('              </adjust-transform>')

        out.append('            </asset-clip>')
        off_fr += dur_fr                          # next clip starts exactly here

    out.append('          </spine>')
    out.append('        </sequence>')
    out.append('      </project>')
    out.append('    </event>')
    out.append('  </library>')
    out.append('</fcpxml>')
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# Multi-clip SEQUENCE handoff (sequence.py's NLE window).
# Unlike to_fcpxml() — which splits ONE source into retimed spans — this lays
# SEVERAL distinct sources end-to-end on a single spine, each trimmed to its
# [inn, out] source range. Hard cuts only; no zoom/retime (that's studio.py).
# Each source becomes its own <asset>+<format>; the sequence format is the FIRST
# clip's (matching sequence.py's export, which pads everything into that frame).
# Timeline times (offset/duration) use the sequence timebase; each clip's `start`
# (source in-point) uses that asset's own timebase. A handoff, frame-aligned per
# track — the human fine-tunes cuts in the NLE; nothing is re-encoded.
# ----------------------------------------------------------------------------
def to_fcpxml_sequence(spec):
    """spec: {name, clips: [{src, name, w, h, fps, dur, inn, out, audio}]}.
    Returns an FCPXML 1.9 document laying the trimmed clips on one spine."""
    clips = spec.get("clips") or []
    if not clips:
        raise ValueError("sequence has no clips")
    name = spec.get("name") or "sequence"
    # sequence (timeline) timebase: explicit seq_fps wins, else the first clip's fps.
    seq_tb = int(round(float(spec.get("seq_fps") or clips[0]["fps"]))) or 30
    seq_w, seq_h = int(clips[0]["w"]), int(clips[0]["h"])

    fmts, fmt_lines, fid = {}, [], [0]
    def fmt_for(w, h, fps):
        tb = int(round(float(fps))) or 30
        key = (w, h, tb)
        if key not in fmts:
            fid[0] += 1; rid = "f%d" % fid[0]; fmts[key] = rid
            fmt_lines.append(
                '    <format id="%s" name="FFVideoFormat%dp%d" frameDuration="1/%ds" '
                'width="%d" height="%d" colorSpace="1-1-1 (Rec. 709)"/>'
                % (rid, h, tb, tb, w, h))
        return fmts[key]

    seq_fmt = fmt_for(seq_w, seq_h, seq_tb)
    asset_lines, assets = [], []
    for k, c in enumerate(clips):
        cfps = float(c["fps"]); ctb = int(round(cfps)) or 30
        afmt = fmt_for(int(c["w"]), int(c["h"]), cfps)
        aid = "a%d" % k
        has_a = "1" if c.get("audio") else "0"
        src = os.path.abspath(os.path.expanduser(c["src"]))
        asset_lines.append(
            '    <asset id="%s" name=%s start="0s" duration="%s" hasVideo="1" '
            'hasAudio="%s" format="%s" videoSources="1" audioSources="%s">'
            % (aid, quoteattr(c.get("name", "clip")), _rational(float(c["dur"]), ctb),
               has_a, afmt, has_a))
        asset_lines.append('      <media-rep kind="original-media" src=%s/>'
                           % quoteattr("file://" + src))
        asset_lines.append('    </asset>')
        assets.append((aid, afmt, ctb))

    # Chain timeline offsets/durations in INTEGER FRAMES so the spine is exactly
    # contiguous — independently rounding each float offset can leave ±1-frame
    # gaps/overlaps (same fix as to_fcpxml). Each clip's timeline length is
    # (out-inn) on the sequence timebase.
    dur_fr = [_frames(float(c["out"]) - float(c["inn"]), seq_tb) for c in clips]
    seq_fr = sum(dur_fr)
    _fr = lambda n: ("%d/%ds" % (n, seq_tb)) if n else "0s"
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE fcpxml>',
           '<fcpxml version="1.9">', '  <resources>']
    out += fmt_lines + asset_lines
    out += ['  </resources>', '  <library>',
            '    <event name=%s>' % quoteattr(name + " (handoff)"),
            '      <project name=%s>' % quoteattr(name),
            '        <sequence format="%s" duration="%s" tcStart="0s" tcFormat="NDF">'
            % (seq_fmt, _fr(seq_fr)), '          <spine>']
    off_fr = 0
    for k, c in enumerate(clips):
        aid, afmt, ctb = assets[k]
        out.append(
            '            <asset-clip ref="%s" offset="%s" name=%s start="%s" '
            'duration="%s" format="%s" tcFormat="NDF"/>'
            % (aid, _fr(off_fr), quoteattr(c.get("name", "clip")),
               _rational(float(c["inn"]), ctb), _fr(dur_fr[k]), afmt))
        off_fr += dur_fr[k]
    out += ['          </spine>', '        </sequence>', '      </project>',
            '    </event>', '  </library>', '</fcpxml>']
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _load_json(path):
    if not path:
        return []
    with open(path) as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Emit an FCPXML 1.9 handoff (Resolve/FCP/Premiere) for the "
                    "screen-studio-alternative zoom+speed edit spec."
    )
    ap.add_argument("src", help="source video path")
    ap.add_argument("--regions", help="JSON: [{t0,t1,z,cx,cy}]")
    ap.add_argument("--speed-segments", help="JSON: [{t0,t1,speed}]")
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--duration", type=float, default=None,
                    help="source duration (s); inferred from spec if omitted")
    ap.add_argument("--ramp", type=float, default=0.5,
                    help="cosine ease ramp (s), must match the render spec")
    ap.add_argument("--name", default=None)
    ap.add_argument("--out", default=None, help="output .fcpxml (default: stdout)")
    a = ap.parse_args(argv)

    spec = {
        "src": a.src,
        "width": a.width,
        "height": a.height,
        "fps": a.fps,
        "ramp": a.ramp,
        "regions": _load_json(a.regions),
        "speed_segments": _load_json(a.speed_segments),
    }
    if a.duration is not None:
        spec["duration"] = a.duration
    if a.name:
        spec["name"] = a.name

    doc = to_fcpxml(spec)
    if a.out:
        with open(a.out, "w") as f:
            f.write(doc)
        print("wrote %s" % a.out, file=sys.stderr)
    else:
        sys.stdout.write(doc)


# ----------------------------------------------------------------------------
# Self-verification: run with no args -> synthetic spec, parse-check, sanity math.
# ----------------------------------------------------------------------------
def _self_test():
    from xml.dom.minidom import parseString

    W, H, fps = 1920, 1080, 30
    # Synthetic 10s source: one zoom region (2x on a corner) + one 2x speed seg.
    spec = {
        "src": "/tmp/fake_source.mp4",
        "width": W, "height": H, "fps": fps, "duration": 10.0, "ramp": 0.5,
        "regions": [{"t0": 2.0, "t1": 5.0, "z": 2.0, "cx": 1600, "cy": 300}],
        "speed_segments": [{"t0": 6.0, "t1": 10.0, "speed": 2.0}],
    }
    doc = to_fcpxml(spec)

    # 1) Well-formed XML?
    parseString(doc)
    print("PASS: document parses as well-formed XML")

    # 2) Retimed duration math. Source 10s: [0,6] at 1x = 6s out, [6,10] at 2x =
    #    2s out -> total 8s out.
    segs, out_dur, s2o = build_speed_map(spec["speed_segments"], 10.0)
    assert abs(out_dur - 8.0) < 1e-9, out_dur
    print("PASS: retimed output duration = %.3fs (expected 8.000s)" % out_dur)

    # 3) Region time remap: region [2,5] is before the sped span, so 1x -> stays
    #    [2,5] in output time.
    assert abs(s2o(2.0) - 2.0) < 1e-9 and abs(s2o(5.0) - 5.0) < 1e-9
    # A time inside the sped span: s2o(8.0) = 6 + (8-6)/2 = 7.0
    assert abs(s2o(8.0) - 7.0) < 1e-9, s2o(8.0)
    print("PASS: s2o remap correct (8.0s src -> %.3fs out)" % s2o(8.0))

    # 4) Every keyframe & timept & clip time is frame-aligned (denominator==fps).
    import re
    bad = []
    for m in re.finditer(r'(?:time|value|offset|duration|start)="([^"]+)"', doc):
        t = m.group(1)
        if t == "0s":
            continue
        mm = re.fullmatch(r'(\d+)/(\d+)s', t)
        if not mm:
            continue  # non-time attribute value (e.g. "2.000000 2.000000")
        num, den = int(mm.group(1)), int(mm.group(2))
        if den != fps:
            bad.append(t)
    assert not bad, "non-frame-aligned times: %s" % bad
    print("PASS: all rational times share the fps timebase (1/%d s)" % fps)

    # 5) Spine: one clip per contiguous span. [0,6]@1x and [6,10]@2x -> 2 clips.
    nclips = doc.count("<asset-clip ")
    assert nclips == 2, nclips
    print("PASS: spine split into %d asset-clips (expected 2)" % nclips)

    # 6) The sped clip carries a timeMap; the 1x clip does not.
    assert doc.count("<timeMap>") == 1, doc.count("<timeMap>")
    print("PASS: exactly one <timeMap> (on the 2x clip)")

    # 7) Zoom region falls in clip 1 -> an adjust-transform with scale+position.
    assert "<adjust-transform>" in doc
    assert 'name="scale"' in doc and 'name="position"' in doc
    # Sanity on baked easing: peak scale reaches z=2 at the hold.
    assert 'value="2.000000 2.000000"' in doc
    print("PASS: adjust-transform present; baked scale reaches z=2.0 at hold")

    # 8) Keyframe count across the ramp is dense (cosine baked, not 2 endpoints).
    nkeys = doc.count("<keyframe ")
    assert nkeys >= 8, nkeys
    print("PASS: %d baked keyframes (dense cosine, not endpoint-only)" % nkeys)

    # 9) Multi-clip sequence handoff (to_fcpxml_sequence): two distinct sources,
    #    different resolutions, one trimmed asset-clip each on a single spine.
    seq = to_fcpxml_sequence({
        "name": "seq", "seq_fps": 60,
        "clips": [
            {"src": "/tmp/a.mp4", "name": "a", "w": 1920, "h": 1080, "fps": 30,
             "dur": 10.0, "inn": 1.0, "out": 3.0, "audio": True},
            {"src": "/tmp/b.mp4", "name": "b", "w": 640, "h": 480, "fps": 30,
             "dur": 4.0, "inn": 0.0, "out": 1.5, "audio": False},
        ]})
    parseString(seq)
    assert seq.count("<asset ") == 2, seq.count("<asset ")
    assert seq.count("<asset-clip ") == 2, seq.count("<asset-clip ")
    # total timeline = (3-1) + (1.5-0) = 3.5s on the 60fps sequence timebase = 210/60s
    assert 'duration="210/60s"' in seq, "sequence duration wrong"
    assert 'FFVideoFormat1080p60' in seq, "sequence format should be 60p (seq_fps)"
    print("PASS: to_fcpxml_sequence — 2 assets, 2 asset-clips, 3.5s @60p timeline")

    # 9) Spine is EXACTLY contiguous: offset[k]+duration[k] == offset[k+1] for
    #    every adjacent pair, and the last clip ends at the sequence duration.
    #    Independent of the generator math: re-parse the emitted XML and walk the
    #    spine in frames. Catches the ±1-frame gap/overlap rounding bug.
    dom = parseString(doc)
    seq = dom.getElementsByTagName("sequence")[0]
    seq_end = _frames_attr(seq.getAttribute("duration"), fps)
    clips = dom.getElementsByTagName("asset-clip")
    cursor = 0
    for c in clips:
        off = _frames_attr(c.getAttribute("offset"), fps)
        dur = _frames_attr(c.getAttribute("duration"), fps)
        assert off == cursor, (
            "spine gap/overlap: clip offset %d != expected %d" % (off, cursor))
        cursor += dur
    assert cursor == seq_end, (
        "spine end %d != sequence duration %d" % (cursor, seq_end))
    print("PASS: spine is exactly contiguous (no gap/overlap; end == seq dur)")

    print("\n----- generated FCPXML -----\n")
    print(doc)


def _frames_attr(t, fps):
    """Parse an FCPXML time attribute ('N/Ds' or '0s') back to integer frames."""
    if not t or t == "0s":
        return 0
    import re as _re
    m = _re.fullmatch(r"(\d+)/(\d+)s", t)
    if not m:
        raise ValueError("unparseable time attr: %r" % t)
    num, den = int(m.group(1)), int(m.group(2))
    return int(round(num * fps / den))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        _self_test()
    else:
        main()
