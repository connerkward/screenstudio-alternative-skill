#!/usr/bin/env python3
"""polish.py — Screen Studio-style post-production for screen recordings, headless.

Features (each optional, composable):
  --speedup            auto-compress idle spans (freezedetect -> retime). No events needed.
  --zoom               auto-zoom on click clusters (needs events.jsonl)
  --keys               keystroke overlay chips (needs events.jsonl)
  --smooth-cursor      replace jittery cursor with eased synthetic one
                       (best on footage recorded WITHOUT a cursor; needs events.jsonl)
  --vertical           additional 9:16 output following the action (needs events.jsonl
                       for the action path; falls back to center crop without)

Pipeline order (fixed): cursor/keys overlays in ORIGINAL time -> retime (speedup)
-> zoom/vertical in OUTPUT time (event timestamps are remapped through the retime map).
Freeze detection always runs on the ORIGINAL input so overlay pixels don't mask stillness.

Usage:
  polish.py in.mp4 [--events in.events.jsonl] [--out out.mp4] [features...]
            [--idle-speed 6] [--zoom-level 1.8] [--vertical-out out-vertical.mp4]
"""
import argparse, json, math, os, re, shutil, subprocess, sys, tempfile

FONT = "/System/Library/Fonts/Helvetica.ttc"

def load_font(size):
    """Sans-serif TrueType, portable across macOS / Linux / Windows. Falls back to
    Pillow's bundled default (scalable in Pillow >= 10) so the pipeline runs anywhere."""
    from PIL import ImageFont
    for p in (FONT,                                                  # macOS
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",     # Debian/Ubuntu
              "/usr/share/fonts/dejavu/DejaVuSans.ttf",              # Fedora/Arch
              "C:/Windows/Fonts/arial.ttf",                          # Windows
              "DejaVuSans.ttf", "Arial.ttf"):                        # PATH/CWD fallback
        try: return ImageFont.truetype(p, size)
        except OSError: continue
    try: return ImageFont.load_default(size)                         # Pillow >= 10
    except TypeError: return ImageFont.load_default()

# ---------------------------------------------------------------- utils
def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return r

def probe(path):
    r = run(["ffprobe","-v","error","-select_streams","v:0",
             "-show_entries","stream=width,height,r_frame_rate",
             "-show_entries","format=duration","-of","json",path])
    j = json.loads(r.stdout); s = j["streams"][0]
    num,den = s["r_frame_rate"].split("/")
    return {"w":s["width"],"h":s["height"],"fps":float(num)/float(den),
            "dur":float(j["format"]["duration"]),
            "has_audio": bool(json.loads(run(["ffprobe","-v","error","-select_streams","a",
                "-show_entries","stream=index","-of","json",path]).stdout).get("streams"))}

def to_gif(src, out, fps=18, width=720):
    """Two-pass palette GIF (palettegen → paletteuse) — far better quality/size than a
    naive single-pass gif. fps is dropped to ~18 and width capped (Lanczos downscale)
    because GIF is for README/social embeds, not full playback."""
    pal = out + ".palette.png"
    vf = "fps=%d,scale=%d:-1:flags=lanczos" % (fps, width)
    run(["ffmpeg","-y","-v","error","-i",src,"-vf",vf+",palettegen=stats_mode=diff",pal])
    run(["ffmpeg","-y","-v","error","-i",src,"-i",pal,
         "-lavfi",vf+"[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",out])
    try: os.remove(pal)
    except OSError: pass
    return out

def cursor_at(moves, t):
    """(x,y) of the cursor at source time t, linearly interpolated from the move track."""
    if not moves: return None
    lo, hi = 0, len(moves)-1
    while lo < hi:
        m = (lo+hi)//2
        if moves[m][0] < t: lo = m+1
        else: hi = m
    a = moves[max(0,lo-1)]; b = moves[lo]
    d = b[0]-a[0] or 1.0; f = max(0.0, min(1.0, (t-a[0])/d))
    return (a[1]+(b[1]-a[1])*f, a[2]+(b[2]-a[2])*f)

def load_events(path, vid):
    """Parse events.jsonl; map display points -> video pixels."""
    header, moves, clicks, keys = None, [], [], []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            e = json.loads(line)
            if e.get("type") == "header": header = e; continue
            t = e.get("t")
            if e.get("type") == "move":  moves.append((t, e["x"], e["y"]))
            elif e.get("type") == "down": clicks.append((t, e["x"], e["y"]))
            elif e.get("type") == "key":  keys.append((t, e["key"]))
    # event coords are display POINTS; header carries pointsW/H (real logger)
    # or just w/h (synthetic fixtures, where points == pixels)
    disp_w = header["display"].get("pointsW", header["display"]["w"]) if header else vid["w"]
    disp_h = header["display"].get("pointsH", header["display"]["h"]) if header else vid["h"]
    sx, sy = vid["w"]/disp_w, vid["h"]/disp_h
    moves  = [(t, x*sx, y*sy) for t,x,y in moves]
    clicks = [(t, x*sx, y*sy) for t,x,y in clicks]
    return {"moves":moves, "clicks":clicks, "keys":keys}

# ---------------------------------------------------------------- speedup
def detect_freezes(path, noise=0.003, min_d=1.5):
    r = subprocess.run(["ffmpeg","-i",path,"-vf",f"freezedetect=n={noise}:d={min_d}",
                        "-map","0:v","-f","null","-"], capture_output=True, text=True)
    starts = [float(m) for m in re.findall(r"freeze_start: ([\d.]+)", r.stderr)]
    ends   = [float(m) for m in re.findall(r"freeze_end: ([\d.]+)",   r.stderr)]
    spans = list(zip(starts, ends))
    if starts and len(starts) == len(ends)+1:            # freeze runs to EOF (guard empty starts)
        spans.append((starts[-1], None))
    return spans

def idle_from_events(ev, dur, min_gap=1.5):
    """Idle spans = gaps with no input activity (clicks, keys, or actual cursor
    displacement). More reliable than pixels: a moving cursor IS activity even
    when it's too small for freezedetect; an idle hand is idle even mid-render."""
    acts = [t for t,_,_ in ev["clicks"]] + [t for t,_ in ev["keys"]]
    px, py, pt = None, None, None
    for t,x,y in ev["moves"]:
        if px is not None and abs(x-px)+abs(y-py) > 1.5: acts.append(t)
        px, py, pt = x, y, t
    acts = sorted(set([0.0] + acts + [dur]))
    spans = []
    for a,b in zip(acts, acts[1:]):
        if b-a >= min_gap: spans.append((a,b))
    return spans

def intersect_spans(A, B, dur):
    out = []
    for a0,a1 in A:
        a1 = dur if a1 is None else a1
        for b0,b1 in B:
            b1 = dur if b1 is None else b1
            lo, hi = max(a0,b0), min(a1,b1)
            if hi-lo > 0.5: out.append((lo,hi))
    return sorted(out)

def subtract_spans(A, B):
    """A minus B: drop any portion of A-intervals covered by a B-interval (splits as
    needed). Used so idle/speed-up spans never overlap zoom regions — a moment you zoom
    in to highlight must not also be fast-forwarded (see studio.analyze)."""
    out = []
    for a0, a1 in A:
        pieces = [(a0, a1)]
        for b0, b1 in B:
            b1 = a1 if b1 is None else b1
            nxt = []
            for s, e in pieces:
                if b1 <= s or b0 >= e: nxt.append((s, e)); continue   # no overlap
                if b0 > s: nxt.append((s, b0))                        # left remainder
                if b1 < e: nxt.append((b1, e))                        # right remainder
            pieces = nxt
        out += pieces
    return [(s, e) for s, e in out if e - s > 1e-6]

def build_segments(dur, freezes, idle_speed, lead=0.4):
    """[(t0,t1,speed)] covering the timeline. Keep `lead` seconds of each freeze
    edge at 1x so the cut doesn't feel abrupt."""
    segs, cur = [], 0.0
    for s,e in freezes:
        e = dur if e is None else e
        s2, e2 = s+lead, e-lead
        if e2 - s2 < 0.5: continue                       # too short to bother
        if s2 > cur: segs.append((cur, s2, 1.0))
        segs.append((s2, e2, idle_speed))
        cur = e2
    if cur < dur: segs.append((cur, dur, 1.0))
    return segs

def remap_t(segs):
    """Return f(orig_t)->out_t through the retime map."""
    table = []   # (t0, t1, speed, out0)
    out = 0.0
    for t0,t1,sp in segs:
        table.append((t0,t1,sp,out)); out += (t1-t0)/sp
    def f(t):
        for t0,t1,sp,o in table:
            if t <= t1 or (t0,t1,sp,o)==table[-1]:
                return o + max(0.0, min(t,t1)-t0)/sp
        return out
    return f, out

def atempo_chain(speed):
    parts = []
    while speed > 2.0: parts.append("atempo=2.0"); speed /= 2.0
    parts.append(f"atempo={speed:.6f}")
    return ",".join(parts)

def apply_speedup(inp, outp, segs, vid):
    v_parts, a_parts, vmaps, amaps = [], [], [], []
    for i,(t0,t1,sp) in enumerate(segs):
        v_parts.append(f"[0:v]trim=start={t0:.4f}:end={t1:.4f},setpts=(PTS-STARTPTS)/{sp}[v{i}]")
        vmaps.append(f"[v{i}]")
        if vid["has_audio"]:
            a_parts.append(f"[0:a]atrim=start={t0:.4f}:end={t1:.4f},asetpts=PTS-STARTPTS,{atempo_chain(sp)}[a{i}]")
            amaps.append(f"[a{i}]")
    n = len(segs)
    fc = ";".join(v_parts+a_parts) + f";{''.join(vmaps)}concat=n={n}:v=1:a=0[vo]"
    maps = ["-map","[vo]"]
    if vid["has_audio"]:
        fc += f";{''.join(amaps)}concat=n={n}:v=0:a=1[ao]"
        maps += ["-map","[ao]"]
    run(["ffmpeg","-y","-loglevel","error","-i",inp,"-filter_complex",fc,*maps,
         "-c:v","libx264","-crf","18","-pix_fmt","yuv420p",outp])

# ---------------------------------------------------------------- zoom
def ease_expr(t0, t1, a, b, T="t"):
    """Cosine-eased scalar from a at t0 to b at t1, as an ffmpeg expr of time var T."""
    if abs(b-a) < 1e-6 or t1 <= t0: return f"{b:.5f}"
    return (f"({a:.5f}+({b-a:.5f})*(0.5-0.5*cos(PI*({T}-{t0:.4f})/({t1-t0:.4f}))))")

def piecewise(kfs, T="t"):
    """kfs: [(t, value)] sorted. Build nested if() expr: eased between keyframes.
    T is the ffmpeg time variable ('t' for crop/overlay, '(on/FPS)' for zoompan)."""
    expr = f"{kfs[-1][1]:.5f}"
    for i in range(len(kfs)-1, 0, -1):
        t0,v0 = kfs[i-1]; t1,v1 = kfs[i]
        expr = f"if(lt({T},{t1:.4f}),{ease_expr(t0,t1,v0,v1,T)},{expr})"
    return f"if(lt({T},{kfs[0][0]:.4f}),{kfs[0][1]:.5f},{expr})"

def zoom_regions(clicks, vid, level, dur, gap=2.5, pre=0.45, post=1.0, ramp=0.55,
                 level_cap=2.6, keys=None, moves=None):
    """Auto-detect editable zoom regions → [{t0,t1,z,cx,cy}] (t0..t1 = zoom span in
    source time; cx,cy = center in video px). Two activity sources:
      • click clusters — span holds through any typing that follows the last click;
      • standalone TYPING bursts (no nearby click) — activity-aware zoom centred on the
        cursor position during the burst, so e.g. tabbing into a field and typing still
        zooms. (Scroll/drag aren't in the event log, so they aren't covered.)
    Edge-aware level: corner targets zoom harder so they can be centered."""
    W,H = vid["w"], vid["h"]
    key_t = sorted(t for t,_ in (keys or []))
    def edge_z(cx,cy):
        need = max(W/(2*max(1,min(cx,W-cx))), H/(2*max(1,min(cy,H-cy))))
        return round(max(level, min(need, level_cap)), 3)
    regs = []
    if clicks:
        clusters, cur = [], [clicks[0]]
        for c in clicks[1:]:
            (cur.append(c) if c[0]-cur[-1][0] <= gap else (clusters.append(cur), cur := [c]))
        clusters.append(cur)
        last_end = 0.0
        for ci,cl in enumerate(clusters):
            next_start = clusters[ci+1][0][0] if ci+1 < len(clusters) else dur
            last_click = cl[-1][0]
            typed = [kt for kt in key_t if last_click-0.3 <= kt < next_start-0.3]
            hold_until = max([last_click] + typed)
            cx = sum(c[1] for c in cl)/len(cl); cy = sum(c[2] for c in cl)/len(cl)
            t0 = max(last_end+0.02, cl[0][0]-pre-ramp)
            t1 = min(dur-0.02, hold_until+post+ramp)
            regs.append({"t0":round(t0,3),"t1":round(t1,3),"z":edge_z(cx,cy),"cx":round(cx,1),"cy":round(cy,1)})
            last_end = t1
    if moves and key_t:                                    # activity-aware typing-burst zooms
        bursts, b = [], [key_t[0]]
        for kt in key_t[1:]:
            (b.append(kt) if kt-b[-1] <= 1.5 else (bursts.append(b), b := [kt]))
        bursts.append(b)
        for bb in bursts:
            bs, be = bb[0], bb[-1]
            if be-bs < 0.4 and len(bb) < 3: continue        # ignore a stray keypress
            cxcy = cursor_at(moves, bs)
            if not cxcy: continue
            t0 = max(0.02, bs-pre-ramp); t1 = min(dur-0.02, be+post+ramp)
            if any(t0 < r["t1"] and t1 > r["t0"] for r in regs): continue  # already covered by a click cluster
            regs.append({"t0":round(t0,3),"t1":round(t1,3),"z":edge_z(*cxcy),
                         "cx":round(cxcy[0],1),"cy":round(cxcy[1],1)})
        regs.sort(key=lambda r: r["t0"])
    return regs

def keyframes_from_regions(regions, vid):
    """Editable regions → eased z/x/y keyframes for the ffmpeg path (cosine ramps).
    The high-quality renderer (render.py) uses these regions directly with springs."""
    W,H = vid["w"], vid["h"]
    zk, xk, yk = [(0.0,1.0)], [(0.0,W/2)], [(0.0,H/2)]
    for r in sorted(regions, key=lambda r: float(r["t0"])):
        t0,t1 = float(r["t0"]), float(r["t1"]); z = float(r.get("z",2.0))
        if t1-t0 < 0.2: continue
        rmp = min(0.5, (t1-t0)/3)
        cx = max(W/(2*z), min(W-W/(2*z), float(r.get("cx",W/2))))
        cy = max(H/(2*z), min(H-H/(2*z), float(r.get("cy",H/2))))
        t_in = max(zk[-1][0]+0.01, t0)
        zk += [(t_in,1.0),(t_in+rmp,z),(t1-rmp,z),(t1,1.0)]
        xk += [(t_in,xk[-1][1]),(t_in+rmp,cx),(t1-rmp,cx),(t1,W/2)]
        yk += [(t_in,yk[-1][1]),(t_in+rmp,cy),(t1-rmp,cy),(t1,H/2)]
    return {"z":zk,"x":xk,"y":yk}

def zoom_keyframes(clicks, vid, level, dur, **kw):
    keys = kw.pop("keys", None)
    regs = zoom_regions(clicks, vid, level, dur, keys=keys, **kw)
    return keyframes_from_regions(regs, vid) if regs else None

def zoom_filter(kfs, vid):
    # NOTE: animated scale(eval=frame) -> crop is BROKEN in ffmpeg (the link
    # reinit on every size change wedges crop's per-frame x/y evaluation —
    # verified empirically; constant-size scale works, varying does not).
    # zoompan avoids it entirely: fixed output size, per-output-frame exprs.
    fps = round(vid["fps"])
    T = f"(on/{fps})"            # zoompan has no 't'; on = output frame index
    z  = piecewise(kfs["z"], T)
    cx = piecewise(kfs["x"], T)
    cy = piecewise(kfs["y"], T)
    W,H = vid["w"], vid["h"]
    # zoompan x/y = top-left of the zoom window in INPUT pixels
    return (f"zoompan=z='{z}':"
            f"x='max(0,min(iw-iw/zoom,({cx})-iw/zoom/2))':"
            f"y='max(0,min(ih-ih/zoom,({cy})-ih/zoom/2))':"
            f"d=1:s={W}x{H}:fps={fps}")

def vertical_filter(kfs, vid, out_w=1080, out_h=1920):
    """9:16 vertical that BOTH follows the action (crop, constant size, animate x — the
    only reliable animated-crop form) AND zooms during clicks (zoompan within the strip,
    which the crop has already centered on the target, so the zoom is just strip-centered).
    crop uses 't'; zoompan uses on/FPS — same graph, same fps, frames align."""
    W,H = vid["w"], vid["h"]
    cw = int(H*9/16//2*2)                                # 9:16 strip width at full height
    cx = piecewise(kfs["x"], "t") if kfs else f"{W/2}"   # crop follow-x (target during zoom)
    crop = f"crop={cw}:{H}:x='max(0,min(iw-{cw},({cx})-{cw}/2))':y=0"
    if not kfs:
        return f"{crop},scale={out_w}:{out_h}"
    fps = round(vid["fps"]); T = f"(on/{fps})"
    z  = piecewise(kfs["z"], T)
    cy = piecewise(kfs["y"], T)                          # crop y=0, so cy unchanged in strip
    # zoom centered horizontally on the strip (target is already strip-centered by crop),
    # vertically on cy; zoompan outputs the final 9:16 frame directly.
    return (f"{crop},zoompan=z='{z}':"
            f"x='(iw-iw/zoom)/2':"
            f"y='max(0,min(ih-ih/zoom,({cy})-ih/zoom/2))':"
            f"d=1:s={out_w}x{out_h}:fps={fps}")

# ---------------------------------------------------------------- keys
def key_chips(keys, gap=0.9, hold=1.4, maxlen=24, max_chips=80):
    """Accumulating chips: each keystroke extends the visible text (like real
    typing), a >gap pause or maxlen starts a fresh chip. Returns
    [(t0, t1, text)] where each entry shows `text` from t0 until t1."""
    if not keys: return []
    groups, cur = [], [keys[0]]
    for t,k in keys[1:]:
        if t-cur[-1][0] <= gap and len(cur) < maxlen: cur.append((t,k))
        else: groups.append(cur); cur = [(t,k)]
    groups.append(cur)
    chips = []
    for g in groups:
        for i,(t,_) in enumerate(g):
            t_next = g[i+1][0] if i+1 < len(g) else t+hold
            chips.append((t, t_next, "".join(k for _,k in g[:i+1])))
    if len(chips) > max_chips:   # typing-heavy clip: fall back to per-group chips
        chips = [(g[0][0], g[-1][0]+hold, "".join(k for _,k in g)) for g in groups]
    return chips

def render_chips(chips, vid, tmp):
    """Render each chip as a PNG (PIL) — no drawtext/libfreetype dependency.
    Returns [(png_path, t0, t1)]."""
    from PIL import Image, ImageDraw, ImageFont
    font = load_font(44)
    out = []
    for i,(t0,t1,text) in enumerate(chips):
        bbox = font.getbbox(text); tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
        pad = 22
        img = Image.new("RGBA", (tw+2*pad, th+2*pad+8), (0,0,0,0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0,0,img.width-1,img.height-1], 14, fill=(0,0,0,150))
        d.text((pad-bbox[0], pad-bbox[1]), text, font=font, fill=(255,255,255,255))
        p = os.path.join(tmp, f"chip{i}.png"); img.save(p)
        out.append((p, t0, t1))
    return out

# ---------------------------------------------------------------- cursor
def smooth_positions(moves, fps, dur, sigma_frames=4, rest_px=2.0):
    """Resample to frame grid, gaussian-smooth, snap micro-jitter at rest."""
    n = int(dur*fps)+1
    xs, ys = [0.0]*n, [0.0]*n
    if not moves:               # events file with clicks/keys but no move lines (bug B)
        return xs, ys           # nothing to interpolate -> all-zero track, no IndexError
    j = 0
    for i in range(n):
        t = i/fps
        while j < len(moves)-1 and moves[j+1][0] <= t: j += 1
        if j < len(moves)-1 and moves[j+1][0] > moves[j][0]:
            a = (t-moves[j][0])/(moves[j+1][0]-moves[j][0])
            a = max(0.0,min(1.0,a))
            xs[i] = moves[j][1]+(moves[j+1][1]-moves[j][1])*a
            ys[i] = moves[j][2]+(moves[j+1][2]-moves[j][2])*a
        else:
            xs[i], ys[i] = moves[j][1], moves[j][2]
    # gaussian kernel
    k = [math.exp(-0.5*(d/sigma_frames)**2) for d in range(-3*sigma_frames,3*sigma_frames+1)]
    ks = sum(k)
    def smooth(v):
        out = []
        for i in range(len(v)):
            acc = 0.0
            for d,kv in enumerate(k):
                idx = max(0,min(len(v)-1, i+d-3*sigma_frames))
                acc += v[idx]*kv
            out.append(acc/ks)
        return out
    xs, ys = smooth(xs), smooth(ys)
    for i in range(1,n):                                   # rest snap
        if abs(xs[i]-xs[i-1]) < rest_px and abs(ys[i]-ys[i-1]) < rest_px:
            xs[i], ys[i] = xs[i-1], ys[i-1]
    return xs, ys

def render_cursor_track(moves, vid, tmp, idle_fade=1.5):
    """PNG sequence of just the cursor (transparent), eased + fading when idle."""
    from PIL import Image, ImageDraw
    fps, dur = vid["fps"], vid["dur"]
    xs, ys = smooth_positions(moves, fps, dur)
    n = len(xs)
    cdir = os.path.join(tmp,"cursor"); os.makedirs(cdir, exist_ok=True)
    idle = 0.0
    for i in range(n):
        moving = i>0 and (abs(xs[i]-xs[i-1])>0.5 or abs(ys[i]-ys[i-1])>0.5)
        idle = 0.0 if moving else idle + 1/fps
        alpha = 255 if idle < idle_fade else max(0, int(255*(1-(idle-idle_fade)/0.5)))
        img = Image.new("RGBA",(vid["w"],vid["h"]),(0,0,0,0))
        if alpha > 0:
            d = ImageDraw.Draw(img); x,y = xs[i],ys[i]; s=1.4   # slightly larger than native
            pts = [(x,y),(x,y+19*s),(x+5*s,y+15*s),(x+9*s,y+23*s),(x+12*s,y+21*s),(x+8*s,y+14*s),(x+14*s,y+13*s)]
            d.polygon(pts, fill=(0,0,0,alpha), outline=(255,255,255,alpha))
        img.save(f"{cdir}/c{i:05d}.png")
    return cdir

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input"); ap.add_argument("--events"); ap.add_argument("--out")
    ap.add_argument("--speedup", action="store_true"); ap.add_argument("--idle-speed", type=float, default=6.0)
    ap.add_argument("--zoom", action="store_true");    ap.add_argument("--zoom-level", type=float, default=2.0)
    ap.add_argument("--keys", action="store_true")
    ap.add_argument("--smooth-cursor", action="store_true")
    ap.add_argument("--vertical", action="store_true"); ap.add_argument("--vertical-out")
    a = ap.parse_args()
    inp = a.input
    out = a.out or re.sub(r"\.(mp4|mov)$", r".polished.\1", inp)
    vid = probe(inp)
    ev = load_events(a.events, vid) if a.events else None
    need_ev = a.zoom or a.keys or a.smooth_cursor
    if need_ev and not ev: sys.exit("--zoom/--keys/--smooth-cursor need --events <file.jsonl>")

    tmp = tempfile.mkdtemp(prefix="polish-")
    stage = inp

    # freeze detection on ORIGINAL footage (before any overlay)
    segs = None
    if a.speedup:
        pixel_spans = detect_freezes(inp)
        if ev:   # input-gap idle, confirmed by pixels (won't compress animations)
            idle = intersect_spans(idle_from_events(ev, vid["dur"]), pixel_spans, vid["dur"])
        else:
            idle = pixel_spans
        segs = build_segments(vid["dur"], idle, a.idle_speed)
        print(f"[speedup] idle spans: {[(round(s,1), round(e,1) if e else 'EOF') for s,e in idle]}")

    # ---- pass A: smoothed cursor (in original time; it IS part of the scene,
    # so it gets zoomed/cropped with the content — correct). Keystroke chips are
    # NOT here — they're a fixed HUD applied LAST so zoom/crop can't eat them.
    if a.smooth_cursor and ev:
        cdir = render_cursor_track(ev["moves"], vid, tmp)
        stage_a = os.path.join(tmp,"a.mp4")
        run(["ffmpeg","-y","-loglevel","error","-i",stage,
             "-framerate",str(vid["fps"]),"-i",f"{cdir}/c%05d.png",
             "-filter_complex","[0:v][1:v]overlay=0:0[vo]","-map","[vo]",
             *(["-map","0:a"] if vid["has_audio"] else []),
             "-c:v","libx264","-crf","18","-pix_fmt","yuv420p",stage_a])
        stage = stage_a

    # ---- pass B: retime ---------------------------------------------------
    if segs:
        stage_b = os.path.join(tmp,"b.mp4")
        apply_speedup(stage, stage_b, segs, vid)
        stage = stage_b
        f_remap, new_dur = remap_t(segs)
        print(f"[speedup] {vid['dur']:.1f}s -> {new_dur:.1f}s")
        if ev:   # remap event times into output timeline
            ev = {"moves":[(f_remap(t),x,y) for t,x,y in ev["moves"]],
                  "clicks":[(f_remap(t),x,y) for t,x,y in ev["clicks"]],
                  "keys":[(f_remap(t),k) for t,k in ev["keys"]]}
        vid = {**vid, "dur": new_dur}

    base = stage   # post cursor+speedup, pre-zoom: feeds BOTH horizontal and vertical

    # keystroke chips as a fixed bottom-center HUD on a finished WxH video
    chip_pngs = render_chips(key_chips(ev["keys"]), vid, tmp) if (a.keys and ev) else []
    if a.keys and ev: print(f"[keys] {len(chip_pngs)} chips")
    def add_chip_hud(src_video, dst_video, margin=70):
        if not chip_pngs:
            if src_video != dst_video: shutil.copy(src_video, dst_video)
            return
        inputs = ["-i", src_video]
        for png,t0,t1 in chip_pngs:
            inputs += ["-loop","1","-t",f"{t1+0.1:.3f}","-i",png]
        fc, cur = [], "[0:v]"
        for i,(png,t0,t1) in enumerate(chip_pngs):
            nxt = f"[m{i}]" if i < len(chip_pngs)-1 else "[vo]"
            fc.append(f"{cur}[{i+1}:v]overlay=x=(main_w-overlay_w)/2:"
                      f"y=main_h-overlay_h-{margin}:enable='between(t,{t0:.3f},{t1:.3f})':eof_action=pass{nxt}")
            cur = nxt
        run(["ffmpeg","-y","-loglevel","error",*inputs,"-filter_complex",";".join(fc),
             "-map","[vo]",*(["-map","0:a"] if vid["has_audio"] else []),
             "-c:v","libx264","-crf","18","-pix_fmt","yuv420p",dst_video])

    # ---- horizontal: zoom (pass C) then chip HUD --------------------------
    kfs = None
    if (a.zoom or a.vertical) and ev:
        kfs = zoom_keyframes(ev["clicks"], vid, a.zoom_level, vid["dur"], keys=ev["keys"])
        if kfs: print(f"[zoom] {sum(1 for i in range(1,len(kfs['z'])) if kfs['z'][i][1]>1.0 and kfs['z'][i-1][1]<=1.0)} zoom-ins")
    h_stage = base
    if a.zoom and kfs:
        h_stage = os.path.join(tmp,"c.mp4")
        run(["ffmpeg","-y","-loglevel","error","-i",base,"-vf",zoom_filter(kfs, vid),
             "-c:v","libx264","-crf","18","-pix_fmt","yuv420p",
             *(["-c:a","copy"] if vid["has_audio"] else []), h_stage])
    add_chip_hud(h_stage, out)
    print(f"wrote {out}")

    # ---- vertical: crop+zoom-follow then chip HUD (at 1080x1920) -----------
    if a.vertical:
        vout = a.vertical_out or re.sub(r"\.(mp4|mov)$", r".vertical.\1", out)
        v_stage = os.path.join(tmp,"v.mp4")
        run(["ffmpeg","-y","-loglevel","error","-i",base,"-vf",vertical_filter(kfs, vid),
             "-c:v","libx264","-crf","19","-pix_fmt","yuv420p",
             *(["-c:a","copy"] if vid["has_audio"] else []), v_stage])
        add_chip_hud(v_stage, vout)
        print(f"wrote {vout} (1080x1920)")
    shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()
