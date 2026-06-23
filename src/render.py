#!/usr/bin/env python3
"""render.py — non-destructive, single-pass, high-quality camera renderer.

The Screen Studio approach: the recording is never modified. A render SPEC (zoom
regions + spring params + fps + target res) drives a virtual camera that zooms/pans
over the ORIGINAL high-resolution frames, sampled with LANCZOS, and exported to a
SMALLER target — so a zoom still reads ≥1:1 source pixels and stays crisp (the
opposite of cropping a finished, already-downscaled video).

Pipeline (one decode, one encode):
  source (high-res) ──stream──▶ per-output-frame: out_t → src_t (idle time-remap)
                                 → eased camera (z, cx, cy) → crop → LANCZOS → target
                                 → fixed-size synthetic cursor → encode @ fps

Camera: smootherstep ease-in/out per zoom region AND per pan (full ease, no linear
middle — velocity ~0 at both ends, peaks mid-move). Curve selectable (smooth/snappy/
linear). Crisp supersampled cursor, Screen-Studio click ripples + sounds, idle speed-up.

Usage:
  render.py SRC.mp4 --regions regions.json --out OUT.mp4 --aspect 16:9|1:1|9:16
            [--fit cover|contain] [--events E.jsonl] [--ramp 0.5] [--cursor] [--clickfx]
            [--speedup --idle-speed 8 | --speed-segments segs.json]
"""
import argparse, json, math, os, subprocess, sys, importlib.util
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

_CURSOR = None
def cursor_sprite(h=38):
    """Crisp macOS-style arrow pointer: 4× supersampled + LANCZOS down (anti-aliased),
    white outline, soft drop shadow. Returns (RGBA sprite, hotspot_x, hotspot_y)."""
    global _CURSOR
    if _CURSOR is not None: return _CURSOR
    ss = 4; H = h * ss
    pts = [(0,0),(0,0.80),(0.23,0.62),(0.35,0.93),(0.47,0.88),(0.33,0.59),(0.58,0.59)]
    P = [(x*H, y*H) for x,y in pts]; pad = int(0.22*H)
    w = int(0.60*H); sz = (w+2*pad, H+2*pad)
    off = [(x+pad, y+pad) for x,y in P]
    sh = Image.new("RGBA", sz, (0,0,0,0))
    ImageDraw.Draw(sh).polygon([(x+ss*1.2, y+ss*2.0) for x,y in off], fill=(0,0,0,140))
    base = Image.alpha_composite(Image.new("RGBA",sz,(0,0,0,0)), sh.filter(ImageFilter.GaussianBlur(ss*1.6)))
    ImageDraw.Draw(base).polygon(off, fill=(22,22,24,255), outline=(255,255,255,255), width=int(ss*1.7))
    spr = base.resize((sz[0]//ss, sz[1]//ss), Image.LANCZOS)
    _CURSOR = (spr, pad//ss, pad//ss)
    return _CURSOR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("polish", os.path.join(_HERE, "polish.py"))
polish = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(polish)

# ---- encoder selection + robust libx264 fallback ----------------------------
_HAS_VTB = None
def has_videotoolbox():
    """True if this ffmpeg build exposes the h264_videotoolbox encoder (cached)."""
    global _HAS_VTB
    if _HAS_VTB is None:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                               capture_output=True, text=True)
            _HAS_VTB = "h264_videotoolbox" in (r.stdout or "")
        except Exception:
            _HAS_VTB = False
    return _HAS_VTB

# encoder-specific flag sets. videotoolbox is faster (HW), libx264 is the portable
# software fallback. Each value is the list of ffmpeg args that select + tune the codec.
_ENC_FLAGS = {
    "h264_videotoolbox": ["-c:v", "h264_videotoolbox", "-b:v", "12M",
                          "-pix_fmt", "yuv420p"],
    "libx264": ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                "-pix_fmt", "yuv420p"],
}
def encode_raw(raw_path, Tw, Th, fps, out_path):
    """Encode a rawvideo (rgb24) file to H.264. Picks h264_videotoolbox when this
    ffmpeg build has it, else libx264. If videotoolbox returns non-zero, retries the
    identical command with libx264 (-crf 18 -preset medium -pix_fmt yuv420p). Raises
    RuntimeError with captured stderr if every encoder fails."""
    order = ["h264_videotoolbox", "libx264"] if has_videotoolbox() else ["libx264"]
    errors = []
    for enc in order:
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{Tw}x{Th}", "-r", str(fps), "-i", raw_path,
               *_ENC_FLAGS[enc], "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return enc
        errors.append(f"[{enc}] rc={r.returncode}: {(r.stderr or '').strip()}")
    raise RuntimeError("ffmpeg H.264 encode failed for all encoders:\n" + "\n".join(errors))

# ---- frame styling (backdrop / padding / rounded corners / shadow) ----------
BACKDROPS = {  # (top color, bottom color) vertical gradients
    "dark": ((20,22,30),(12,13,18)), "light": ((238,240,244),(214,219,228)),
    "dusk": ((49,28,72),(24,40,86)), "ocean": ((13,34,58),(11,78,98)),
    "warm": ((64,44,28),(104,56,42)), "mint": ((18,52,46),(26,78,66)),
    "slate": ((40,44,56),(22,25,33)),
}
def make_backdrop(spec, Tw, Th):
    c0, c1 = BACKDROPS.get(spec, BACKDROPS["dark"])
    g = np.zeros((Th, Tw, 3), np.float32)
    for ch in range(3): g[:,:,ch] = np.linspace(c0[ch], c1[ch], Th)[:,None]
    return Image.fromarray(g.astype(np.uint8))
def rounded_mask(w, h, rad):
    m = Image.new("L", (w, h), 0); ImageDraw.Draw(m).rounded_rectangle([0,0,w-1,h-1], rad, fill=255)
    return m
def blur_backdrop(src_frame, Tw, Th, radius, fox, foy, cwf, chf, frad, shadow):
    """Screen-Studio 'blur' bg: this output frame's OWN source, scaled to COVER the
    Tw×Th canvas, heavily Gaussian-blurred and darkened ~25%, with the content card's
    drop shadow drawn on top. src_frame is the raw HxWx3 numpy source frame. Animates
    with the video because each output frame passes its own source frame in."""
    Sh, Sw = src_frame.shape[0], src_frame.shape[1]
    # cover-scale the source to fill Tw×Th (overscan), center-crop to exactly Tw×Th
    s = max(Tw / Sw, Th / Sh)
    rw, rh = max(1, int(round(Sw*s))), max(1, int(round(Sh*s)))
    bg = Image.fromarray(src_frame).resize((rw, rh), Image.LANCZOS)
    bx = (rw - Tw)//2; by = (rh - Th)//2
    bg = bg.crop((bx, by, bx+Tw, by+Th))
    bg = bg.filter(ImageFilter.GaussianBlur(radius))
    bg = Image.eval(bg, lambda v: int(v*0.75))            # darken ~25%
    if shadow:
        sh = Image.new("RGBA",(Tw,Th),(0,0,0,0)); soff = int(12*Tw/1280)
        ImageDraw.Draw(sh).rounded_rectangle([fox,foy+soff,fox+cwf,foy+chf+soff], frad, fill=(0,0,0,135))
        bg = Image.alpha_composite(bg.convert("RGBA"), sh.filter(ImageFilter.GaussianBlur(int(24*Tw/1280)))).convert("RGB")
    return bg

# ---- time remap (idle speed-up) ---------------------------------------------
def time_maps(segs):
    """segs: [(src_t0, src_t1, speed)] covering the source timeline.
    Returns (out_to_src, src_to_out, out_dur)."""
    table, out = [], 0.0
    for s0, s1, sp in segs:
        sp = max(1e-6, sp)                       # never divide by zero, whatever the caller passed
        table.append((s0, s1, sp, out)); out += (s1 - s0) / sp
    def o2s(ot):
        for s0, s1, sp, o0 in table:
            seg_out = (s1 - s0) / sp
            if ot <= o0 + seg_out or (s0, s1, sp, o0) == table[-1]:
                return s0 + (ot - o0) * sp
        return table[-1][1]
    def s2o(st):
        for s0, s1, sp, o0 in table:
            if st <= s1 or (s0, s1, sp, o0) == table[-1]:
                return o0 + (max(s0, min(st, s1)) - s0) / sp
        return out
    return o2s, s2o, out

# ---- eased camera trajectory ------------------------------------------------
def ease_curve(a, curve="smooth"):
    """Map normalized progress a∈[0,1] → eased 0..1. Full ease-in-out — velocity ~0
    at BOTH ends, peaks mid-move (no constant-velocity middle). KEEP IN SYNC with
    studio.py easeCurve().
      smooth  → smootherstep 6a^5−15a^4+10a^3 (default; cinematic slow-start/stop)
      snappy  → smootherstep then smoothstep on top → steeper midslope (faster snap)
      linear  → a (constant velocity — the old mechanical feel, for comparison)"""
    if a <= 0: return 0.0
    if a >= 1: return 1.0
    if curve == "linear":
        return a
    s = a * a * a * (a * (a * 6 - 15) + 10)    # smootherstep
    if curve == "snappy":
        # spend less time ramping: square the eased value's complement to sharpen the
        # acceleration/deceleration so the move "snaps" into place faster, still 0-vel ends
        s = s * s * (3 - 2 * s)                 # apply smoothstep on top → steeper midslope
    return s

_PAN_WARP = 0.65
def ease_pan(a):
    """Human-camera-operator pan easing for cx/cy translation (NOT zoom). Asymmetric,
    ease-OUT-dominant: a quick pickup off the start, an EARLY velocity peak (~a=0.27),
    then a long gentle deceleration that softly settles into the final framing — like a
    real operator who gets the head moving then rides it down to a stop. Velocity is
    exactly 0 at BOTH ends (no hard start/stop, no overshoot bounce).

    Construction: smootherstep (which has 0 velocity AND 0 acceleration at both ends)
    evaluated on a front-loaded input warp a**0.65. The warp compresses the early portion
    of progress so the velocity peak lands at ~27% and ~90% of the travel is done by ~65%
    of the time, leaving a long settle tail. Composing smootherstep with the warp preserves
    the zero-velocity ends. KEEP IN SYNC with studio.py easePan()."""
    if a <= 0: return 0.0
    if a >= 1: return 1.0
    w = a ** _PAN_WARP
    return w * w * w * (w * (w * 6 - 15) + 10)    # smootherstep(a**0.65)

def ease_traj(regions, key, rest, fps, dur, ramp=0.5, curve="smooth"):
    """Per-region eased trajectory → per-frame array. FULL ease-in-out (smootherstep
    by default) on every move — zoom rest→v, the in-region PAN v→v1, and v1→rest —
    so velocity is ~0 at both ends and peaks in the middle (no linear middle).
      ramp  : seconds of ease for the zoom-in / zoom-out at a region's edges.
      curve : 'smooth' (smootherstep, default) | 'snappy' | 'linear'.
    A region with a pan target (key+'1') glides across its ENTIRE span with the same
    eased curve — not just a held sub-span — so the pan reads cinematic, not mechanical.
    KEEP IN SYNC with studio.py easeTraj()."""
    n = int(round(dur * fps)) + 1
    arr = [rest] * n
    for r in sorted(regions, key=lambda r: r["o0"]):
        o0, o1, v = r["o0"], r["o1"], r[key]
        v1 = r.get(key + "1", v)               # optional in-region target → camera PANS v→v1
        rmp = min(ramp, (o1 - o0) / 2)
        h0, h1 = o0 + rmp, o1 - rmp            # held span between the ease-in / ease-out
        for i in range(n):
            t = i / fps
            if t < o0 or t > o1: continue
            if key != "z" and v1 != v:                              # PAN: one continuous glide across the WHOLE region (overlaps zoom in/out → no hard stop/beat)
                a = (t - o0) / (o1 - o0) if o1 > o0 else 1.0
                # cx/cy translation uses the human ease-out-dominant pan curve (early
                # velocity peak, long gentle settle); z keeps its own ease (left as-is).
                arr[i] = v + (v1 - v) * (a if curve == "linear" else ease_pan(a))
            elif rmp > 0 and t < o0 + rmp:
                a = (t - o0) / rmp
                b = rest if key == "z" else v                        # z: zoom rest→v ; static cx/cy: hold
                arr[i] = b + (v - b) * ease_curve(a, curve)
            elif rmp > 0 and t > o1 - rmp:
                a = (o1 - t) / rmp
                b = rest if key == "z" else v1                       # z: zoom v1→rest
                arr[i] = b + (v1 - b) * ease_curve(a, curve)
            else:
                arr[i] = v
    return arr

# ---- text callouts (screen-fixed labels, background-aware, like keystroke chips) ----
# A callout = {"t0","t1","text","anchor","size"} in OUTPUT time. Drawn last, on the final
# output frame, so it stays put while the camera zooms/pans. The pill colour adapts to the
# luminance behind it (dark pill on light bg, light pill on dark) — same trick as the click
# ripple — so it's legible over any content. Anchors keep it at the edges, off the content.
def _callout_measure(text, px):
    font = polish.load_font(px)
    bb = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=font)
    padx, pady = int(px * 0.62), int(px * 0.42)
    return (bb[2]-bb[0]) + 2*padx, (bb[3]-bb[1]) + 2*pady, font, bb, padx, pady

def _callout_render(text, bg_lum, font, bb, padx, pady, W, H):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(img)
    if bg_lum > 128: pill, txt = (18, 18, 22, 205), (255, 255, 255, 255)   # light bg → dark pill
    else:            pill, txt = (244, 244, 247, 216), (16, 16, 20, 255)   # dark bg → light pill
    d.rounded_rectangle([0, 0, W-1, H-1], radius=int(H*0.42), fill=pill)
    d.text((padx - bb[0], pady - bb[1]), text, font=font, fill=txt)
    return img

def _callout_pos(anchor, W, H, Tw, Th, m):
    a = (anchor or "bottom").lower()
    horiz = "left" if "left" in a else "right" if "right" in a else "center"
    vert  = "top"  if "top"  in a else "bottom" if "bottom" in a else "middle"
    x = m if horiz == "left" else (Tw-W-m if horiz == "right" else (Tw-W)//2)
    y = m if vert == "top"  else (Th-H-m if vert == "bottom" else (Th-H)//2)
    return max(0, min(x, Tw-W)), max(0, min(y, Th-H))

# ---- main render ------------------------------------------------------------
ASPECTS = {"16:9": (1280, 720), "1:1": (1080, 1080), "9:16": (1080, 1920)}

# ── agent-readable metadata: bake the key "full-zoom" moments into the video ──
# An agent that can't watch video can read these to jump straight to a relevant
# frame: `ffprobe -show_chapters <v>` for in-file chapters, or the richer
# `<v>.moments.json` sidecar (timestamp + what the camera is framed on), then
# `ffmpeg -ss <t> -i <v> -frames:v 1 frame.png` to grab + analyze that moment.
def _build_moments(R, regions, *, Sw, Sh, fps, out_dur):
    moms = [{"t": 0.0, "kind": "overview", "label": "start / overview", "z": 1.0}]
    for i, r in enumerate(R):
        t = round((r["o0"] + r["o1"]) / 2.0, 3)          # middle of the held zoom/pan = representative frame
        lab = (regions[i].get("label") if i < len(regions) else None) or f"zoom {i + 1}"
        is_pan = "cx1" in r or "cy1" in r
        cx = (r["cx"] + r.get("cx1", r["cx"])) / 2.0     # for a pan, frame the midpoint
        cy = (r["cy"] + r.get("cy1", r["cy"])) / 2.0
        moms.append({"t": t, "kind": "pan" if is_pan else "zoom", "label": lab, "z": round(r["z"], 3),
                     "cx": round(cx, 1), "cy": round(cy, 1),
                     "cx_norm": round(cx / Sw, 4), "cy_norm": round(cy / Sh, 4),
                     "in": round(r["o0"], 3), "out": round(r["o1"], 3)})
    moms.sort(key=lambda m: m["t"])
    for m in moms:
        m["frame"] = int(round(m["t"] * fps))
    return moms

def _ffmeta_chapters(moments, out_dur):
    lines = [";FFMETADATA1"]
    ts = [m["t"] for m in moments] + [out_dur]
    for i, m in enumerate(moments):
        start = int(round(m["t"] * 1000)); end = int(round(min(ts[i + 1], out_dur) * 1000))
        if end <= start: end = min(int(out_dur * 1000), start + 1500)
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                  "title=" + str(m["label"]).replace("\n", " ")]
    return "\n".join(lines) + "\n"

def _bake_metadata(out_path, moments, *, src, fps, Sw, Sh, Tw, Th, out_dur):
    side = out_path + ".moments.json"
    json.dump({"video": os.path.basename(out_path), "source": os.path.basename(src),
               "duration": round(out_dur, 3), "fps": fps, "w": Tw, "h": Th,
               "source_w": Sw, "source_h": Sh,
               "howto": "jump to moment.t (seconds) and grab a frame: "
                        "ffmpeg -ss <t> -i <video> -frames:v 1 out.png  · in-file: ffprobe -show_chapters <video>",
               "moments": moments}, open(side, "w"), indent=2)
    meta = out_path + ".ffmeta.txt"; open(meta, "w").write(_ffmeta_chapters(moments, out_dur))
    tmp = out_path + ".chap.mp4"
    rc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", out_path, "-i", meta,
                         "-map_metadata", "1", "-map_chapters", "1", "-codec", "copy", tmp]).returncode
    if rc == 0 and os.path.exists(tmp): os.replace(tmp, out_path)
    else:
        try: os.remove(tmp)
        except OSError: pass
    try: os.remove(meta)
    except OSError: pass
    return side

def render(src, regions, out_path, *, aspect="16:9", fit="cover", events=None, fps=60,
           ramp=0.5, curve="smooth", cursor=False, clickfx=False, keys=False, segs=None,
           bg="none", pad=0.06, radius=18, shadow=True, callouts=None, progress_file=None,
           metadata=True):
    vid = polish.probe(src)
    Sw, Sh, sfps = vid["w"], vid["h"], vid["fps"]
    o2s, s2o, out_dur = time_maps(segs) if segs else (lambda t: t, lambda t: t, vid["dur"])

    # output geometry from aspect ratio
    Tw, Th = ASPECTS.get(aspect, ASPECTS["16:9"])
    out_aspect = Tw / Th
    src_aspect = Sw / Sh
    contain = (fit == "contain")

    # frame styling: backdrop active when bg is a real value. blur is dynamic (the
    # backdrop is this frame's own source, blurred) so its static gradient/shadow are
    # built per-frame; flat gradients precompute their (gradient + shadow) once.
    use_bg   = bool(bg) and bg != "none"
    use_blur = (bg == "blur")
    # CONTENT RECT — the single rectangle the source pixels land in. Without a backdrop
    # it's the whole output frame; with one it's the padded inset (cwf×chf at fox,foy).
    # Both `cover` and `contain` fit the crop into THIS rect (one fit, one resize) so the
    # source aspect is never doubly-squashed and the title bar is never pre-cropped away.
    if use_bg:
        cwf = max(2, int(round(Tw*(1-2*pad)))//2*2); chf = max(2, int(round(Th*(1-2*pad)))//2*2)
        fox = (Tw-cwf)//2; foy = (Th-chf)//2; frad = int(radius*Tw/1280)
        cmask = rounded_mask(cwf, chf, frad)
    else:
        cwf, chf, fox, foy, frad, cmask = Tw, Th, 0, 0, 0, None
    Cw, Ch = cwf, chf                 # content-rect dims = the fit target
    content_aspect = Cw / Ch
    # cover = crop the source to the content-rect aspect and fill it (overscan cut).
    # contain = crop at the SOURCE aspect (nothing cut) and letterbox/pillarbox inside it.
    crop_aspect = src_aspect if contain else content_aspect
    # solid-color base for the letterbox pad when contain runs WITHOUT a backdrop (black);
    # with a backdrop the pad shows the backdrop itself, handled in the per-frame compositor.
    contain_base = Image.new("RGB", (Cw, Ch), (0, 0, 0))

    # precompute the static (gradient) backdrop + shadow once; blur builds its own per-frame.
    frame_bg = None
    if use_bg and not use_blur:
        bd = make_backdrop(bg, Tw, Th)
        if shadow:
            sh = Image.new("RGBA",(Tw,Th),(0,0,0,0)); soff = int(12*Tw/1280)
            ImageDraw.Draw(sh).rounded_rectangle([fox,foy+soff,fox+cwf,foy+chf+soff], frad, fill=(0,0,0,135))
            bd = Image.alpha_composite(bd.convert("RGBA"), sh.filter(ImageFilter.GaussianBlur(int(24*Tw/1280)))).convert("RGB")
        frame_bg = bd
    blur_radius = max(1, int(round(0.04 * Tw)))   # ~0.04*width, scales with resolution

    # regions: convert source-time spans to OUTPUT-time, clamp centers
    R = []
    for r in regions:
        z = float(r.get("z", 2.0))
        d = {"o0": s2o(float(r["t0"])), "o1": s2o(float(r["t1"])), "z": z,
             "cx": float(r.get("cx", Sw / 2)), "cy": float(r.get("cy", Sh / 2))}
        if "cx1" in r: d["cx1"] = float(r["cx1"])    # pan target → ease_traj glides cx/cy across the hold
        if "cy1" in r: d["cy1"] = float(r["cy1"])
        R.append(d)

    # eased camera trajectories over the output timeline
    zt  = ease_traj(R, "z",  1.0,  fps, out_dur, ramp, curve)
    cxt = ease_traj(R, "cx", Sw/2, fps, out_dur, ramp, curve)
    cyt = ease_traj(R, "cy", Sh/2, fps, out_dur, ramp, curve)
    nframes = len(zt)

    cur_xs = cur_ys = None; clicks = []
    callouts = list(callouts or [])
    if (cursor or clickfx or keys) and events:
        ev = polish.load_events(events, vid)
        if cursor: cur_xs, cur_ys = polish.smooth_positions(ev["moves"], sfps, vid["dur"])
        if clickfx: clicks = [(t, x, y) for t, x, y in ev["clicks"]]
        if keys:   # keystroke caption = ONE bottom-center window per burst whose text grows
            callouts += [{"t0": s2o(g["t0"]), "t1": s2o(g["t1"]), "anchor": "bottom", "size": 0.055,
                          "steps": [(s2o(st), tx) for st, tx in g["steps"]]}
                         for g in polish.key_caps(ev["keys"])]
    RIPPLE = 0.5   # seconds; soft expanding ring per click

    # if click sounds are needed, render video to a temp then mux audio
    vid_target = out_path
    tmp_v = out_path + ".silent.mp4" if (clickfx and clicks) else None
    if tmp_v: vid_target = tmp_v
    # decode source as rgb24 stream; processed frames are buffered to a temp rawvideo
    # file so the encode can be retried on a different encoder (videotoolbox→libx264)
    # with the identical command if the first encoder returns non-zero.
    dec = subprocess.Popen(["ffmpeg","-v","error","-i",src,"-f","rawvideo","-pix_fmt","rgb24","-"],
                           stdout=subprocess.PIPE, bufsize=Sw*Sh*3)
    raw_path = vid_target + ".rgb24.raw"
    raw_f = open(raw_path, "wb")
    frame_bytes = Sw*Sh*3
    cur_src_idx = -1; cur_frame = None
    try:
        for i in range(nframes):
            if progress_file and i % 4 == 0:                 # real progress for the UI ring
                # atomic write: a concurrent reader (the /progress poll) must never
                # see a torn JSON file mid-write -> write tmp then rename. (bug E)
                try:
                    _pt = progress_file + ".tmp"
                    open(_pt, "w").write('{"i":%d,"n":%d}' % (i, nframes))
                    os.replace(_pt, progress_file)
                except Exception: pass
            src_t = o2s(i / fps)
            want = min(int(src_t * sfps), int(vid["dur"]*sfps))
            while cur_src_idx < want:
                buf = dec.stdout.read(frame_bytes)
                if not buf or len(buf) < frame_bytes:
                    buf = None; break
                cur_frame = np.frombuffer(buf, np.uint8).reshape(Sh, Sw, 3)
                cur_src_idx += 1
            if cur_frame is None: break
            z = max(1.0, zt[i])
            cw = min(Sw, Sw / z); ch = cw / crop_aspect
            if ch > Sh: ch = Sh; cw = ch * crop_aspect
            cx = min(Sw - cw/2, max(cw/2, cxt[i])); cy = min(Sh - ch/2, max(ch/2, cyt[i]))
            x0 = int(round(cx - cw/2)); y0 = int(round(cy - ch/2))
            x1 = min(Sw, x0 + int(round(cw))); y1 = min(Sh, y0 + int(round(ch)))
            crop = cur_frame[y0:y1, x0:x1]
            cwpx, chpx = (x1 - x0), (y1 - y0)
            # Fit the crop into the CONTENT RECT (Cw×Ch) exactly once. `img` is always a
            # Cw×Ch image; (pox,poy) is the source's offset INSIDE that rect, (sx,sy) the
            # source→content scale. cover fills the rect (overscan), contain letterboxes.
            if contain:
                # whole crop visible, padded to the content rect (one edge touches).
                dw = Cw; dh = int(round(dw * chpx / cwpx))
                if dh > Ch: dh = Ch; dw = int(round(dh * cwpx / chpx))
                dw = max(1, dw); dh = max(1, dh)
                content = Image.fromarray(crop).resize((dw, dh), Image.LANCZOS)
                pox = (Cw - dw) // 2; poy = (Ch - dh) // 2
                img = contain_base.copy(); img.paste(content, (pox, poy))
                sx = dw / cwpx; sy = dh / chpx
            else:
                # cover: the crop already matches the content-rect aspect → fills it.
                img = Image.fromarray(crop).resize((Cw, Ch), Image.LANCZOS)
                sx = Cw / cwpx; sy = Ch / chpx; pox = poy = 0
            # click ripple — soft expanding ring; color is background-aware (dark ring on
            # light bg, light ring on dark) so it never gets lost. Coords are content-local.
            if clicks:
                arr = np.asarray(img); ov = None
                for ct, ccx, ccy in clicks:
                    p = (src_t - ct) / RIPPLE
                    if 0 <= p <= 1:
                        rx = (ccx - x0) * sx + pox; ry = (ccy - y0) * sy + poy
                        if -60 < rx < Cw+60 and -60 < ry < Ch+60:
                            if ov is None:
                                ov = Image.new("RGBA", img.size, (0,0,0,0)); od = ImageDraw.Draw(ov)
                            r = 9 + p*46; a = (1-p)
                            bx, by = int(min(max(rx,0),Cw-1)), int(min(max(ry,0),Ch-1))
                            lum = int(arr[by, bx].mean())
                            col = (30,30,34) if lum > 128 else (255,255,255)   # contrast w/ bg
                            od.ellipse([rx-r,ry-r,rx+r,ry+r], fill=col+(int(55*a),))
                            od.ellipse([rx-r,ry-r,rx+r,ry+r], outline=col+(int(210*a),), width=3)
                if ov is not None:
                    img = Image.alpha_composite(img.convert("RGBA"),
                          ov.filter(ImageFilter.GaussianBlur(0.6))).convert("RGB")
            if cur_xs is not None:
                ox = (cur_xs[min(cur_src_idx, len(cur_xs)-1)] - x0) * sx + pox
                oy = (cur_ys[min(cur_src_idx, len(cur_ys)-1)] - y0) * sy + poy
                if -50 < ox < Cw+50 and -50 < oy < Ch+50:
                    spr, hx, hy = cursor_sprite()   # crisp fixed-size cursor (no balloon)
                    img.paste(spr, (int(round(ox-hx)), int(round(oy-hy))), spr)
            # composite: drop the Cw×Ch content onto the backdrop (rounded + shadow). The
            # content rect already carries the right aspect — NO second resize/squash here.
            if use_bg:
                if use_blur:
                    base = blur_backdrop(cur_frame, Tw, Th, blur_radius,
                                         fox, foy, cwf, chf, frad, shadow)
                else:
                    base = frame_bg.copy()
                base.paste(img, (fox, foy), cmask); img = base
            if callouts:
                ot = i / fps; fr = None
                for c in callouts:
                    if not (c["t0"] <= ot <= c["t1"]): continue
                    fade = max(0.0, min(1.0, (ot - c["t0"]) / 0.3, (c["t1"] - ot) / 0.3))
                    if fade <= 0: continue
                    if c.get("steps"):                       # keystroke caption: text accumulates within the window
                        text = c["steps"][0][1]
                        for st, tx in c["steps"]:
                            if st <= ot: text = tx
                            else: break
                    else:
                        text = c.get("text", "")
                    if not text: continue
                    cpx = max(12, int(c.get("size", 0.036) * Th))
                    W, H, font, bb, padx, pady = _callout_measure(text, cpx)
                    cx0, cy0 = _callout_pos(c.get("anchor", "bottom"), W, H, Tw, Th, int(0.045*Th))
                    if fr is None: fr = np.asarray(img if img.mode == "RGB" else img.convert("RGB"))
                    sub = fr[cy0:cy0+H, cx0:cx0+W]
                    lum = float(sub.mean()) if sub.size else 128.0
                    co = _callout_render(text, lum, font, bb, padx, pady, W, H)
                    if fade < 1.0:
                        co.putalpha(co.split()[3].point(lambda v: int(v * fade)))
                    img.paste(co, (cx0, cy0), co)
            raw_f.write(img.tobytes())
    finally:
        try: raw_f.close()
        except Exception: pass
        dec.terminate()
    # encode the buffered frames (videotoolbox when available, libx264 fallback)
    try:
        encode_raw(raw_path, Tw, Th, fps, vid_target)
    finally:
        try: os.remove(raw_path)
        except OSError: pass

    # click sounds: place the real recorded click sample at each click (output time), mux
    if tmp_v and clicks:
        import wave
        sr = 44100; n = int(out_dur*sr) + sr//2; a = np.zeros(n, np.float32)
        cw_path = os.path.join(ROOT, "assets", "click.wav")
        if os.path.exists(cw_path):
            with wave.open(cw_path,"rb") as w:
                csr = w.getframerate(); raw = w.readframes(w.getnframes())
                samp = np.frombuffer(raw, "<i2").astype(np.float32)/32768.0
                if w.getnchannels()==2: samp = samp.reshape(-1,2).mean(1)
                if csr != sr:   # resample to 44.1k (linear)
                    samp = np.interp(np.linspace(0,len(samp),int(len(samp)*sr/csr),endpoint=False),
                                     np.arange(len(samp)), samp)
            samp *= 0.8
            for ct, _, _ in clicks:
                i = int(s2o(ct)*sr)
                if 0 <= i < n:
                    seg = samp[:min(len(samp), n-i)]; a[i:i+len(seg)] += seg
        else:   # --clickfx asked for click sounds but the sample is missing (bug D)
            print("render.py: warning: %s not found; click track will be silent"
                  % cw_path, file=sys.stderr)
        a = np.clip(a, -1, 1); pcm = (a*32767).astype("<i2")
        wav = out_path + ".clicks.wav"
        with wave.open(wav,"wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
        subprocess.run(["ffmpeg","-y","-v","error","-i",tmp_v,"-i",wav,"-c:v","copy",
                        "-c:a","aac","-b:a","160k","-shortest",out_path], check=True)
        os.remove(tmp_v); os.remove(wav)
    result = {"w": Tw, "h": Th, "fps": fps, "frames": nframes, "dur": round(out_dur, 2)}
    if metadata:
        moments = _build_moments(R, regions, Sw=Sw, Sh=Sh, fps=fps, out_dur=out_dur)
        result["moments_file"] = _bake_metadata(out_path, moments, src=src, fps=fps,
                                                Sw=Sw, Sh=Sh, Tw=Tw, Th=Th, out_dur=out_dur)
        result["moments"] = moments
    return result

def build_segs(src, events, idle_speed):
    vid = polish.probe(src)
    pixel = polish.detect_freezes(src)
    if events:
        ev = polish.load_events(events, vid)
        idle = polish.intersect_spans(polish.idle_from_events(ev, vid["dur"]), pixel, vid["dur"])
    else:
        idle = pixel
    return polish.build_segments(vid["dur"], idle, idle_speed)

def build_speed_segs(dur, spans):
    """Explicit per-segment speeds → full-coverage [(t0,t1,speed)] (1× between)."""
    segs, cur = [], 0.0
    for s in sorted(spans, key=lambda s: float(s["t0"])):
        t0, t1, sp = max(cur, float(s["t0"])), float(s["t1"]), max(1.0, float(s.get("speed", 8)))
        if t1 - t0 < 0.3: continue
        if t0 > cur: segs.append((cur, t0, 1.0))
        segs.append((t0, t1, sp)); cur = t1
    if cur < dur: segs.append((cur, dur, 1.0))
    return segs

def clip_segs_to_trim(segs, dur, trim_in, trim_out):
    """Restrict a full-coverage seg list to the source window [trim_in, trim_out] so the
    time-remap (time_maps) only spans the trimmed range — output 0 maps to trim_in and
    out_dur reflects only the kept range. trim_out<=0 → source end. If segs is None we
    synthesize a single 1× seg over the window so trim works without any speed-up."""
    lo = max(0.0, float(trim_in or 0.0))
    hi = float(trim_out) if (trim_out and trim_out > 0) else dur
    hi = max(lo + 1e-3, min(hi, dur))
    if lo <= 1e-6 and hi >= dur - 1e-6:
        return segs                                   # no real trim → leave segs untouched
    base = segs if segs else [(0.0, dur, 1.0)]
    out = []
    for s0, s1, sp in base:
        a, b = max(s0, lo), min(s1, hi)
        if b - a > 1e-6: out.append((a, b, sp))
    if not out: out = [(lo, hi, 1.0)]
    return out

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("--regions", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--aspect", default="16:9", choices=list(ASPECTS))
    ap.add_argument("--fit", default="cover", choices=["cover", "contain"],
                    help="cover = crop-to-fill the output aspect (default, may cut non-16:9 "
                         "sources); contain = fit whole source inside frame + letterbox/pad")
    ap.add_argument("--events")
    ap.add_argument("--fps", type=int, default=60); ap.add_argument("--ramp", type=float, default=0.5)
    ap.add_argument("--curve", default="smooth", choices=["smooth", "snappy", "linear"],
                    help="camera easing curve: smooth=smootherstep ease-in-out (default), snappy=steeper, linear=constant-velocity (mechanical)")
    ap.add_argument("--cursor", action="store_true"); ap.add_argument("--clickfx", action="store_true")
    ap.add_argument("--keys", action="store_true", help="keystroke chips (auto bottom callouts; needs --events)")
    ap.add_argument("--speedup", action="store_true"); ap.add_argument("--idle-speed", type=float, default=8.0)
    ap.add_argument("--speed-segments")
    ap.add_argument("--bg", default="none", choices=["none", *BACKDROPS, "blur"],
                    help="backdrop behind the padded/rounded content: 'none', a gradient "
                         "(dark/light/dusk/ocean/warm/mint/slate), or 'blur' (the source "
                         "frame itself, scaled to cover + Gaussian-blurred + darkened)")
    ap.add_argument("--pad", type=float, default=0.06)
    ap.add_argument("--radius", type=float, default=18); ap.add_argument("--no-shadow", action="store_true")
    ap.add_argument("--callouts", help="JSON [{t0,t1,text,anchor,size}] of screen-fixed text labels (OUTPUT time); anchor e.g. top-left/top/bottom-right/center")
    ap.add_argument("--progress-file")
    ap.add_argument("--trim-in", type=float, default=0.0, help="source clip in-point (seconds); demo starts here")
    ap.add_argument("--trim-out", type=float, default=0.0, help="source clip out-point (seconds; 0 = source end); demo ends here")
    ap.add_argument("--no-metadata", action="store_true",
                    help="skip baking agent-readable moment chapters + <out>.moments.json sidecar (on by default)")
    a = ap.parse_args()
    regions = json.load(open(a.regions)) if os.path.exists(a.regions) else json.loads(a.regions)
    callouts = (json.load(open(a.callouts)) if os.path.exists(a.callouts) else json.loads(a.callouts)) if a.callouts else None
    if a.speed_segments:
        spans = json.load(open(a.speed_segments)) if os.path.exists(a.speed_segments) else json.loads(a.speed_segments)
        segs = build_speed_segs(polish.probe(a.src)["dur"], spans)
    elif a.speedup:
        segs = build_segs(a.src, a.events, a.idle_speed)
    else:
        segs = None
    if a.trim_in or a.trim_out:                       # restrict the time-remap to the trimmed source window
        _dur = polish.probe(a.src)["dur"]
        segs = clip_segs_to_trim(segs, _dur, a.trim_in, a.trim_out)
    r = render(a.src, regions, a.out, aspect=a.aspect, fit=a.fit, events=a.events, fps=a.fps,
               ramp=a.ramp, curve=a.curve, cursor=a.cursor, clickfx=a.clickfx, keys=a.keys, segs=segs,
               bg=a.bg, pad=a.pad, radius=a.radius, shadow=not a.no_shadow, callouts=callouts, progress_file=a.progress_file,
               metadata=not a.no_metadata)
    print(json.dumps(r))
