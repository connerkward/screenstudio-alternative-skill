#!/usr/bin/env python3
"""sequence.py — rudimentary multi-clip NLE window (the sibling of studio.py).

studio.py edits ONE recording (zoom/speed/cursor effects). sequence.py is the other
half: arrange SEVERAL clips end-to-end on a single track, trim each, reorder by drag,
and export the concatenation — Windows-Movie-Maker-simple. Hard cuts only; no per-clip
effects (that's studio.py's job).

LIVE preview plays straight through the sequence in the browser: one <video> element
whose src + seek follow the playhead across clip boundaries. "Export" concatenates the
trimmed clips with ffmpeg (each normalized to the first clip's frame, silent audio
synthesized for clips that have none) into one downloadable mp4.

Run:  python3 sequence.py [clip1.mp4 clip2.mov ... | folder]
      (clips can also be added in-browser; CLI args just seed the timeline)
"""
import http.server, json, os, socket, subprocess, sys, urllib.parse, importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(ROOT, ".studio-out"); os.makedirs(OUT, exist_ok=True)
CLIPDIR = os.path.join(OUT, "seqclips"); os.makedirs(CLIPDIR, exist_ok=True)
def _load(name, fn):
    s = importlib.util.spec_from_file_location(name, os.path.join(_HERE, fn))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
polish = _load("polish", "polish.py")
render = _load("render", "render.py")   # reuse encoder selection / fallback
fcpxml = _load("fcpxml", "fcpxml.py")   # reuse the FCPXML handoff writer

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
CLIPS = {}            # id -> {path,name,w,h,fps,dur,has_audio}
_next_id = [0]
TOTAL_US = [1]        # output-duration of the in-progress export, for /progress

def add_clip(path):
    path = os.path.abspath(path)
    if not os.path.isfile(path): return None
    try: vid = polish.probe(path)
    except Exception: return None
    cid = "c%d" % _next_id[0]; _next_id[0] += 1
    CLIPS[cid] = {"path": path, "name": os.path.basename(path), "w": vid["w"], "h": vid["h"],
                  "fps": vid["fps"], "dur": vid["dur"], "has_audio": vid["has_audio"]}
    return cid

def seed(args):
    files = []
    for a in args:
        if os.path.isdir(a):
            files += [os.path.join(a, f) for f in sorted(os.listdir(a))
                      if os.path.splitext(f)[1].lower() in VIDEO_EXT]
        elif os.path.splitext(a)[1].lower() in VIDEO_EXT:
            files.append(a)
    return [c for c in (add_clip(f) for f in files) if c]

def clip_json(cid):
    c = CLIPS[cid]
    return {"id": cid, "name": c["name"], "dur": round(c["dur"], 3), "w": c["w"],
            "h": c["h"], "fps": c["fps"], "audio": c["has_audio"]}

def thumb(cid):
    c = CLIPS.get(cid)
    if not c: return None
    tp = os.path.join(CLIPDIR, cid + ".jpg")
    if not os.path.isfile(tp):
        ss = max(0.0, min(c["dur"] * 0.1, c["dur"] - 0.05))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{ss:.3f}", "-i", c["path"],
                        "-frames:v", "1", "-vf", "scale=320:-1", tp], capture_output=True)
    return tp if os.path.isfile(tp) else None

def export(p):
    items = p.get("clips", [])
    if not items: return {"error": "No clips to export."}
    metas = []
    for it in items:
        c = CLIPS.get(it.get("id"))
        if not c: return {"error": "Unknown clip id: " + str(it.get("id"))}
        inn = max(0.0, float(it.get("inn", 0)))
        out = min(c["dur"], float(it.get("out", c["dur"])))
        if out - inn < 0.05: return {"error": "Clip '%s' trimmed too short." % c["name"]}
        metas.append((c, inn, out))
    # Output frame: 1080p by default, orientation from the chosen aspect (the client
    # pre-selects the one matching the first clip). Clips are scaled-to-fit + padded into it.
    W, H = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}.get(
        p.get("aspect", "16:9"), (1920, 1080))
    fps = 60   # export at 60fps by default; lower-fps sources are frame-duped by the fps filter
    inputs, vlab, aud = [], [], []          # ffmpeg -i args; per-clip video input idx; audio idx
    idx = 0
    for c, inn, out in metas:
        inputs += ["-ss", f"{inn:.3f}", "-t", f"{out-inn:.3f}", "-i", c["path"]]
        vlab.append(idx); aud.append(idx if c["has_audio"] else None); idx += 1
    for k, (c, inn, out) in enumerate(metas):     # synth silent audio for clips with none
        if aud[k] is None:
            inputs += ["-f", "lavfi", "-t", f"{out-inn:.3f}",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            aud[k] = idx; idx += 1
    parts = []
    for k in range(len(metas)):
        parts.append(f"[{vlab[k]}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},"
                     f"format=yuv420p,setpts=PTS-STARTPTS[v{k}]")
        parts.append(f"[{aud[k]}:a]aresample=44100,asetpts=PTS-STARTPTS[a{k}]")
    parts.append("".join(f"[v{k}][a{k}]" for k in range(len(metas)))
                 + f"concat=n={len(metas)}:v=1:a=1[outv][outa]")
    filt = ";".join(parts)
    TOTAL_US[0] = max(1, int(sum(out - inn for _, inn, out in metas) * 1e6))
    out_path = os.path.join(OUT, "sequence.mp4")
    prog = os.path.join(OUT, "seqprogress.txt")
    try: os.remove(prog)
    except OSError: pass
    enc = "h264_videotoolbox" if render.has_videotoolbox() else "libx264"
    for e in ([enc, "libx264"] if enc != "libx264" else ["libx264"]):
        cmd = ["ffmpeg", "-y", "-v", "error", "-progress", prog, *inputs,
               "-filter_complex", filt, "-map", "[outv]", "-map", "[outa]",
               *render._ENC_FLAGS[e], "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            res = {"video": "/media?out=1&v=" + str(os.path.getmtime(out_path)),
                   "name": "sequence-%dclips.mp4" % len(metas)}
            if p.get("gif"):                     # also transcode the concatenated mp4 -> GIF
                gif = os.path.join(OUT, "sequence.gif")
                try:
                    polish.to_gif(out_path, gif)
                    res["gif"] = "/download?gif=1&v=" + str(os.path.getmtime(gif))
                    res["gifName"] = "sequence-%dclips.gif" % len(metas)
                except Exception as ex:
                    res["gifError"] = str(ex)
            return res
    return {"error": (r.stderr or "ffmpeg failed")[-1500:]}

def export_fcpxml(p):
    """Non-destructive handoff: emit an FCPXML laying the trimmed clips on one spine."""
    items = p.get("clips", [])
    if not items: return {"error": "No clips to export."}
    clips = []
    for it in items:
        c = CLIPS.get(it.get("id"))
        if not c: return {"error": "Unknown clip id: " + str(it.get("id"))}
        clips.append({"src": c["path"], "name": os.path.splitext(c["name"])[0],
                      "w": c["w"], "h": c["h"], "fps": c["fps"], "dur": c["dur"],
                      "audio": c["has_audio"], "inn": max(0.0, float(it.get("inn", 0))),
                      "out": min(c["dur"], float(it.get("out", c["dur"])))})
    try:
        xml = fcpxml.to_fcpxml_sequence({"name": "sequence", "clips": clips, "seq_fps": 60})
    except Exception as e:
        return {"error": "FCPXML export failed: " + str(e)}
    op = os.path.join(OUT, "sequence.fcpxml"); open(op, "w").write(xml)
    return {"file": "/download?fcpxml=1&v=" + str(os.path.getmtime(op)),
            "name": "sequence-%dclips.fcpxml" % len(clips)}

def progress():
    fp = os.path.join(OUT, "seqprogress.txt"); last = 0
    try:
        for line in open(fp):
            if line.startswith("out_time_us="):
                v = line.strip().split("=", 1)[1]
                if v.isdigit(): last = int(v)
    except OSError: pass
    return {"i": last, "n": TOTAL_US[0]}

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _q(self, u): return {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = self._q(u)
        if u.path == "/":         return self._send(200, HTML, "text/html; charset=utf-8")
        if u.path == "/favicon.svg": return self._send(200, FAVICON, "image/svg+xml")
        if u.path == "/favicon.ico": return self._send(204, b"", "image/svg+xml")
        if u.path == "/analyze":  return self._send(200, json.dumps({"clips": [clip_json(c) for c in CLIPS]}))
        if u.path == "/progress": return self._send(200, json.dumps(progress()))
        if u.path == "/thumb":
            tp = thumb(q.get("id", ""))
            if not tp: return self._send(404, b"", "text/plain")
            return self._send(200, open(tp, "rb").read(), "image/jpeg")
        if u.path == "/media":
            if q.get("out"):
                p = os.path.join(OUT, "sequence.mp4")
            else:
                c = CLIPS.get(q.get("id", "")); p = c["path"] if c else None
            if not p or not os.path.isfile(p): return self._send(404, b"not found", "text/plain")
            return self._serve_media(p)
        if u.path == "/download":
            fname = "sequence.fcpxml" if q.get("fcpxml") else "sequence.gif" if q.get("gif") else "sequence.mp4"
            p = os.path.join(OUT, fname)
            if not os.path.isfile(p): return self._send(404, b"not found", "text/plain")
            dl = os.path.basename(q.get("dl", "")) or os.path.basename(p)
            ct = {"fcpxml":"application/xml","gif":"image/gif","mp4":"video/mp4"}[fname.rsplit(".",1)[-1]]
            self.send_response(200); self.send_header("Content-Type", ct)
            self.send_header("Content-Disposition", 'attachment; filename="' + dl + '"')
            b = open(p, "rb").read(); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        self._send(404, b"404", "text/plain")
    def _serve_media(self, p):
        size = os.path.getsize(p); rng = self.headers.get("Range")
        with open(p, "rb") as f:
            if rng and rng.startswith("bytes="):
                s, _, e = rng[6:].partition("-")
                try:
                    start = int(s) if s else 0
                    end = min(int(e) if e else size - 1, size - 1)
                except ValueError:
                    start, end = 0, size - 1
                if start > end or start >= size:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                f.seek(start); data = f.read(end - start + 1)
                self.send_response(206); self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            else:
                data = f.read(); self.send_response(200)
                self.send_header("Content-Type", "video/mp4"); self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(size)); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        u = urllib.parse.urlparse(self.path); q = self._q(u)
        n = int(self.headers.get("Content-Length", 0))
        if u.path == "/upload":
            name = os.path.basename(q.get("name", "clip.mp4")) or "clip.mp4"
            stem, ext = os.path.splitext(name)
            if ext.lower() not in VIDEO_EXT:
                self.rfile.read(n); return self._send(400, json.dumps({"error": "unsupported file type " + ext}))
            dst = os.path.join(CLIPDIR, name); i = 1
            while os.path.exists(dst): dst = os.path.join(CLIPDIR, f"{stem}-{i}{ext}"); i += 1
            with open(dst, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk: break
                    f.write(chunk); left -= len(chunk)
            cid = add_clip(dst)
            if not cid: return self._send(400, json.dumps({"error": "could not read video " + name}))
            return self._send(200, json.dumps(clip_json(cid)))
        body = self.rfile.read(n).decode() if n else "{}"
        if u.path == "/export": return self._send(200, json.dumps(export(json.loads(body))))
        if u.path == "/fcpxml": return self._send(200, json.dumps(export_fcpxml(json.loads(body))))
        self._send(404, b"404", "text/plain")

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

FAVICON = (  # teal tile + three stacked clip bars (sequence motif)
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
  '<rect x="1" y="1" width="30" height="30" rx="8" fill="#5fd0c0"/>'
  '<g fill="#06231f"><rect x="7" y="8" width="14" height="4" rx="1.5"/>'
  '<rect x="7" y="14" width="18" height="4" rx="1.5"/><rect x="7" y="20" width="11" height="4" rx="1.5"/></g>'
  '</svg>')

HTML = r"""<!doctype html><html><head><meta charset=utf-8><title>sequence</title>
<link rel=icon type="image/svg+xml" href="/favicon.svg">
<style>
:root{--bg:#0e0f13;--panel:#171922;--line:#262a36;--ink:#e7e9ef;--dim:#8a90a2;--acc:#5fd0c0}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,Inter,system-ui,sans-serif;user-select:none;overflow:hidden}
.app{height:100vh;display:flex;flex-direction:column;gap:10px;padding:12px 16px;max-width:1320px;margin:0 auto}
.topbar{display:flex;align-items:center;gap:10px;flex:none}
h1{font-size:16px;font-weight:650;letter-spacing:-.02em;margin:0}h1 .muted{font-weight:400;color:var(--dim)}
.tbtools{margin-left:auto;display:flex;align-items:center;gap:8px}
.tbtn{background:var(--panel);border:1px solid var(--line);color:var(--ink);border-radius:8px;height:30px;padding:0 12px;font-size:12px;cursor:pointer}.tbtn:hover{border-color:var(--acc)}
.tbexp{background:var(--acc);color:#06231f;border:1px solid var(--acc);border-radius:8px;height:32px;padding:0 18px;font-size:13px;font-weight:700;cursor:pointer}.tbexp:disabled{opacity:.55;cursor:wait}
.tbstat{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:240px}
.ring{width:28px;height:28px;flex:none}.ring .rbg{fill:none;stroke:var(--line);stroke-width:3.4}
.ring .rfg{fill:none;stroke:var(--acc);stroke-width:3.4;stroke-linecap:round;transform:rotate(-90deg);transform-origin:50% 50%;transition:stroke-dashoffset .18s linear}
.main{flex:1;min-height:0;background:#07080b;border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;align-items:center;justify-content:center;position:relative}
/* two stacked videos (double-buffer): both centered, only .show is visible */
.pv{position:absolute;inset:0;margin:auto;max-width:100%;max-height:100%;width:auto;height:auto;background:#000;opacity:0;pointer-events:none}
.pv.show{opacity:1;z-index:2}
.empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;color:var(--dim);text-align:center}
.empty.hide{display:none}
.bigadd{background:var(--acc);color:#06231f;border:0;border-radius:10px;padding:12px 22px;font-size:14px;font-weight:700;cursor:pointer}
.editor{flex:none;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.transport{display:flex;align-items:center;gap:10px;margin-bottom:9px}
.play{background:var(--acc);color:#06231f;border:0;border-radius:8px;width:40px;height:34px;font-size:15px;cursor:pointer;font-weight:700}
.time{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums;min-width:96px}
.addbtn{background:#10121a;border:1px solid var(--line);color:var(--acc);border-radius:8px;height:30px;padding:0 13px;cursor:pointer;font-size:12px;font-weight:600}.addbtn:hover{border-color:var(--acc)}
.tzoom{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:13px}.tzoom input{width:90px;accent-color:var(--acc)}
.outsel{display:flex;align-items:center;gap:7px;color:var(--dim);font-size:12px}
.seg{display:flex;gap:5px}.seg button{background:#10121a;border:1px solid var(--line);color:var(--dim);border-radius:7px;padding:5px 9px;cursor:pointer;font-size:12px}.seg button.on{color:var(--ink);border-color:var(--acc)}
.hint{color:var(--dim);font-size:11px;margin-left:auto;text-align:right}
/* beat-cut submode: breadcrumb hierarchy + control bar */
.submodetab{background:#10121a;border:1px solid var(--line);color:var(--dim);border-radius:999px;height:26px;padding:0 12px;font-size:11px;cursor:pointer;display:flex;align-items:center;margin-left:6px}
.submodetab:hover{border-color:var(--acc);color:var(--ink)}
.submodetab.on{background:#0e1a1a;border-color:var(--acc);color:var(--acc)}
h1 .pmode{font-weight:650}
h1 #crumb .sep{color:var(--dim);margin:0 7px;font-weight:400}
h1 #crumb .sub{color:var(--acc);font-weight:650}
.beatbar{display:none;align-items:center;gap:10px;margin:0 0 9px;padding:8px 11px;background:#0e1a1a;border:1px solid #1f3b38;border-radius:9px;flex-wrap:wrap}
.beatbar.on{display:flex}
.beatbar .bcl{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.07em}
.beatbar input#bpm{width:76px;background:#0c0e15;border:1px solid var(--line);color:var(--ink);border-radius:7px;height:30px;padding:0 9px;font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
.beatbar .bcdim{color:var(--dim);font-size:11px;font-variant-numeric:tabular-nums}
.bcgo{background:var(--acc);color:#06231f;border:0;border-radius:8px;height:30px;padding:0 14px;font-size:12px;font-weight:700;cursor:pointer}.bcgo:active{transform:translateY(1px)}
#tap{min-width:54px}#tap.flash{background:#0e1a1a;border-color:var(--acc);color:var(--acc)}
.track.beatlock .clip .h{opacity:.15;cursor:not-allowed}
.track.beatlock .clip .h::after{background:#ffffff33;height:42%}
.track.beatlock .clip.trimmed .trimflag{background:#5fd0c0;content:""}
.tlscroll{overflow-x:auto;overflow-y:hidden}
.track{position:relative;min-width:440px;height:88px;background:#0c0e15;border:1px solid var(--line);border-radius:8px;cursor:crosshair}
.track.drop{border-color:var(--acc);background:#0e1a1a}
/* clips are ABSOLUTELY positioned (left/width in px from a frozen px-per-second
   scale). left transitions so neighbors slide smoothly on trim/reorder; the
   dragged clip gets transition:none so it tracks the cursor 1:1 (no jump). */
.clip{position:absolute;top:6px;height:calc(100% - 12px);border-radius:7px;overflow:hidden;background:#1b2030 center/cover no-repeat;box-shadow:0 2px 6px #0007,inset 0 0 0 1px #0006;cursor:grab;display:flex;align-items:flex-end;transition:left .12s ease}
.clip.sel{outline:2px solid var(--acc);outline-offset:1px;box-shadow:0 0 0 4px #5fd0c033,0 2px 8px #0008;z-index:5}
.clip.dragging{opacity:.9;cursor:grabbing;transition:none;z-index:20;box-shadow:0 0 0 4px #5fd0c055,0 6px 18px #000a}
.clip .meta{width:100%;padding:3px 7px;font-size:10px;font-weight:700;color:#fff;background:linear-gradient(transparent,#000a);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;pointer-events:none}
.clip .meta .d{font-weight:500;opacity:.8;margin-left:5px}
.clip .h{position:absolute;top:0;width:12px;height:100%;cursor:ew-resize;z-index:4;background:linear-gradient(#0000,#0000)}
.clip .h.l{left:0}.clip .h.r{right:0}
.clip .h::after{content:"";position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:3px;height:42%;border-radius:2px;background:#ffffff80}
.clip:hover .h::after{background:#fff;height:58%}
.clip .trimflag{position:absolute;top:3px;right:3px;font-size:9px;color:#06231f;background:var(--acc);border-radius:4px;padding:1px 4px;font-weight:800;display:none}
.clip.trimmed .trimflag{display:block}
.ph{position:absolute;top:0;width:2px;height:100%;background:var(--acc);pointer-events:none;z-index:6;box-shadow:0 0 6px #5fd0c0aa}
.toolbar2{display:flex;align-items:center;gap:8px;margin-top:9px;color:var(--dim);font-size:12px}
.delbtn{background:#2a1518;border:1px solid #4a2530;color:#ff9a9a;border-radius:7px;height:28px;padding:0 12px;cursor:pointer;font-size:12px}.delbtn:disabled{opacity:.4;cursor:default}
.err{color:#ff8080;font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap;background:#1a1115;border:1px solid #4a2530;border-radius:8px;padding:10px;margin-top:10px}
#errbox{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:60;max-width:92vw}
@media(max-width:820px),(max-height:560px){html,body{height:auto;overflow:auto}.app{height:auto;min-height:100vh}.main{min-height:240px}.hint{display:none}}
</style></head><body><div class=app>
<div class=topbar>
  <h1><span class=pmode>sequence</span><span id=crumb></span> <span class=muted id=subtitle>· multi-clip editor</span></h1>
  <button class=submodetab id=beatmode title="Beat-cut submode: equal-length, beat-aligned cuts for Instagram">▸ beat-cut</button>
  <div class=tbtools>
    <button class=tbtn id=add>＋ Add clips</button>
    <button class=tbtn id=fcx disabled title="Export an editable FCPXML for Resolve / Final Cut / Premiere">Export FCPXML →</button>
    <button class=tbtn id=gif disabled title="Export an animated GIF (palette, 18fps)">GIF</button>
    <button class=tbexp id=go disabled>Export video</button>
    <svg id=pring class=ring viewBox="0 0 36 36" style=display:none><circle class=rbg cx=18 cy=18 r=15.5></circle><circle class=rfg cx=18 cy=18 r=15.5></circle></svg>
    <span class=tbstat id=stat></span>
  </div>
</div>
<div class=main id=main>
  <video class=pv id=vA muted playsinline></video>
  <video class=pv id=vB muted playsinline></video>
  <div class=empty id=empty>
    <div>No clips yet.<br><span style=font-size:12px;opacity:.8>Add video clips, trim &amp; drag to arrange, then export.</span></div>
    <button class=bigadd id=add2>＋ Add clips</button>
  </div>
</div>
<div class=editor>
  <div class=transport>
    <button class=play id=playbtn>▶</button>
    <span class=time id=time>0:00 / 0:00</span>
    <button class=addbtn id=add3>＋ Add</button>
    <div class=outsel title="Output frame — 1080p"><span>1080p</span>
      <div class=seg id=seg_ar><button data-v=16:9 class=on>16:9</button><button data-v=9:16>9:16</button><button data-v=1:1>1:1</button></div></div>
    <label class=tzoom title="Zoom the timeline">⌕<input type=range id=tzoom min=1 max=8 step=0.5 value=1></label>
    <span class=hint id=hint></span>
  </div>
  <div class=beatbar id=beatbar>
    <span class=bcl>BPM</span>
    <input type=number id=bpm min=40 max=300 value=120>
    <button class=addbtn id=tap title="Tap 4+ times in time with the music">Tap</button>
    <span class=bcdim id=tapout></span>
    <span class=bcl>beats / clip</span>
    <div class=seg id=seg_beats><button data-v=0.5>½</button><button data-v=1 class=on>1</button><button data-v=2>2</button><button data-v=4>4</button></div>
    <button class=bcgo id=docut>Cut to beat ✂</button>
    <span class=bcdim id=beatinfo></span>
  </div>
  <div class=tlscroll>
    <div class=track id=track><div class=ph id=ph style=left:0></div></div>
  </div>
  <div class=toolbar2>
    <button class=addbtn id=split title="Split the clip at the playhead (S)">✂ Split</button>
    <button class=delbtn id=del disabled>Delete clip</button>
    <span id=sub>Drag body to reorder · drag edges to trim · shift/⌘-click to multi-select · S splits · ←/→ step a frame · ⌘Z undo / ⌘⇧Z redo · click track to scrub</span>
  </div>
</div>
</div>
<input type=file id=file accept="video/*" multiple style=display:none>
<div id=errbox></div>
<script>
const $=s=>document.querySelector(s), track=$('#track'), ph=$('#ph');
let v=$('#vA'), vNext=$('#vB'), preI=-1;   // v = active (visible) video; vNext double-buffers the upcoming clip
let clips=[];          // [{id,name,dur,w,h,fps,audio, inn,out}]  inn/out = source trim
let META={};           // id -> server clip meta
let sel=-1, cur=-1, playing=false, MIN=0.1;
let selSet=new Set();   // all selected clip indices (sel = anchor / last-clicked). Multi-select via shift/⌘-click.
let clipEls=[], PPS=100, zoomF=1, dragging=false;   // px-per-second scale; frozen mid-drag
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const len=c=>c.out-c.inn;
const total=()=>clips.reduce((s,c)=>s+len(c),0);
function outStart(k){let s=0;for(let j=0;j<k;j++)s+=len(clips[j]);return s;}
function fmt(t){t=Math.max(0,t||0);return Math.floor(t/60)+':'+String(Math.floor(t%60)).padStart(2,'0');}
// ---- map output-time <-> (clip index, source-time) ----
function locate(T){ let s=0; for(let k=0;k<clips.length;k++){const L=len(clips[k]);
  if(T<s+L||k===clips.length-1) return {k, st:clips[k].inn+clamp(T-s,0,L)}; s+=L; } return {k:-1,st:0}; }
function setEmpty(){ const e=clips.length===0; $('#empty').classList.toggle('hide',!e);
  $('#go').disabled=e; $('#fcx').disabled=e; $('#gif').disabled=e; $('#del').disabled=selSet.size===0;
  $('#del').textContent=selSet.size>1?('Delete '+selSet.size+' clips'):'Delete clip'; }
// ---- undo / redo (⌘Z / ⌘⇧Z): snapshot the clip model around every edit ----
let history=[], future=[];
const snapClips=()=>clips.map(c=>({...c}));
const clipsSig=a=>a.map(c=>c.id+':'+c.inn.toFixed(3)+':'+c.out.toFixed(3)).join('|');
function pushHistory(){ history.push(snapClips()); if(history.length>150)history.shift(); future=[]; }   // before a discrete edit
function commitIfChanged(before){ if(clipsSig(before)!==clipsSig(clips)){ history.push(before); if(history.length>150)history.shift(); future=[]; } } // after a drag gesture
function restoreClips(snap){ clips=snap.map(c=>({...c})); selSet=new Set(); sel=-1; drawTrack();
  if(clips.length){ cur=clamp(cur,0,clips.length-1); loadClip(cur,clips[cur].inn,false); } else { cur=-1; } }
function undo(){ if(!history.length){$('#stat').textContent='nothing to undo';return;}
  future.push(snapClips()); restoreClips(history.pop()); $('#stat').textContent='↶ undo'; }
function redo(){ if(!future.length)return; history.push(snapClips()); restoreClips(future.pop()); $('#stat').textContent='↷ redo'; }
// ---- timeline render ----
// buildEls() = structural rebuild (one DOM node per clip, kept in clipEls,
// parallel to `clips`). layout() = cheap reposition: every clip is absolutely
// placed at left=cumulative px, width=len*PPS. Drag handlers mutate the model
// and call layout() (NOT buildEls) so thumbnails/handlers/transitions survive.
function calcPPS(){ const w=$('.tlscroll').clientWidth-16; return Math.max(6,(w/(total()||1)))*zoomF; }
function buildEls(){
  [...track.querySelectorAll('.clip')].forEach(e=>e.remove());
  clipEls=clips.map(c=>{ const el=document.createElement('div'); el.className='clip';
    el.style.backgroundImage='url(/thumb?id='+c.id+')';
    el.innerHTML='<div class="h l"></div><div class=trimflag>trim</div><div class=meta></div><div class="h r"></div>';
    el.onmousedown=e=>{ if(e.target.classList.contains('h'))return; const i=clipEls.indexOf(el);
      if(e.shiftKey){ e.preventDefault(); rangeSel(i); return; }            // shift = contiguous range
      if(e.metaKey||e.ctrlKey){ e.preventDefault(); toggleSel(i); return; } // ⌘/ctrl = toggle one
      startReorder(e,el); };                                               // plain = single-select + reorder
    el.querySelector('.h.l').onmousedown=e=>{e.stopPropagation();startTrim(e,el,'l');};
    el.querySelector('.h.r').onmousedown=e=>{e.stopPropagation();startTrim(e,el,'r');};
    track.appendChild(el); return el; });
}
function layout(skip){
  if(!dragging) PPS=calcPPS();
  let x=8;
  clips.forEach((c,i)=>{ const el=clipEls[i]; const w=Math.max(10,len(c)*PPS);
    if(el!==skip){ el.style.left=x+'px'; el.style.width=w+'px'; }
    el.classList.toggle('sel',selSet.has(i));
    el.classList.toggle('trimmed',c.inn>0.02||c.out<c.dur-0.02);
    el.querySelector('.meta').innerHTML=(i+1)+'. '+c.name+'<span class=d>'+len(c).toFixed(1)+'s</span>';
    x+=w; });
  track.style.width=(x+8)+'px';
  $('#hint').textContent=clips.length+' clip'+(clips.length!==1?'s':'')+' · '+total().toFixed(1)+'s';
  setEmpty();
}
function drawTrack(){ buildEls(); layout(); }     // structural change -> full rebuild
function setSel(i){ sel=i; selSet=new Set(i>=0?[i]:[]); layout(); }     // single-select (or clear with -1)
function toggleSel(i){ if(selSet.has(i))selSet.delete(i); else selSet.add(i); sel=i; layout(); }
function rangeSel(i){ if(sel<0){setSel(i);return;}                       // contiguous range from the anchor
  const a=Math.min(sel,i),b=Math.max(sel,i); selSet=new Set();
  for(let k=a;k<=b;k++)selSet.add(k); layout(); }
// ---- preview playback: DOUBLE-BUFFERED. `v` is the active (visible) video; `vNext`
// holds the upcoming clip pre-seeked + buffered, so crossing a cut is an instant
// element swap instead of a src reload — no boundary hiccup on play-through. ----
function show(){ v.classList.add('show'); vNext.classList.remove('show'); }
function loadInto(el,k,st,play){ if(k<0||k>=clips.length)return;
  el.src='/media?id='+clips[k].id; el.load();
  el.onloadeddata=()=>{ el.currentTime=clamp(st,clips[k].inn,clips[k].out-0.01); if(play)el.play().catch(()=>{}); }; }
function preloadNext(){ const n=cur+1;                          // buffer clip after the current one
  if(n<clips.length){ loadInto(vNext,n,clips[n].inn,false); preI=n; } else preI=-1; }
function refreshPreload(){ preI=-1; preloadNext(); }            // after edits that move the "next" clip
function loadClip(k,st,autoplay){ if(k<0||k>=clips.length){cur=-1;return;}
  cur=k; loadInto(v,k,st,autoplay); show(); refreshPreload(); }
function syncCur(){ const m=(v.src||'').match(/id=([^&]+)/);    // re-sync cur to the loaded clip after reorder/delete
  if(m){ const i=clips.findIndex(c=>c.id===m[1]); if(i>=0)cur=i; } }
function advance(){ const n=cur+1;
  if(n>=clips.length){ if(playing){playing=false;$('#playbtn').textContent='▶';} loadClip(0,clips[0].inn,false); return; }
  if(preI===n){ [v,vNext]=[vNext,v]; cur=n; show(); vNext.pause();   // instant swap to the buffered clip
    if(playing) v.play().catch(()=>{}); refreshPreload(); }
  else loadClip(n,clips[n].inn,playing); }                         // not buffered yet -> fall back (rare)
function seekTo(T){ const L=locate(T); if(L.k<0)return;
  if(L.k!==cur){ loadClip(L.k,L.st,playing); }
  else { v.currentTime=clamp(L.st,clips[L.k].inn,clips[L.k].out-0.01); } }
function curOutTime(){ if(cur<0)return 0; return outStart(cur)+clamp(v.currentTime-clips[cur].inn,0,len(clips[cur])); }
function tick(){
  if(cur>=0 && cur<clips.length){
    const c=clips[cur];
    if(v.currentTime>=c.out-0.04){ advance(); }
    else if(v.currentTime<c.inn-0.04){ v.currentTime=c.inn; }
    const T=curOutTime(), TT=total()||1;
    ph.style.left=(T/TT*100)+'%'; $('#time').textContent=fmt(T)+' / '+fmt(TT);
  }
  requestAnimationFrame(tick);
}
$('#playbtn').onclick=()=>{ if(!clips.length)return;
  if(playing){ playing=false; v.pause(); $('#playbtn').textContent='▶'; }
  else{ playing=true; $('#playbtn').textContent='⏸'; if(cur<0)loadClip(0,clips[0].inn,true); else v.play().catch(()=>{}); } };
// ---- scrub: click empty track background ----
track.addEventListener('mousedown',e=>{ if(!e.target.classList.contains('track')&&e.target!==ph)return;
  setSel(-1); const r=track.getBoundingClientRect();
  seekTo(clamp((e.clientX-r.left-8)/(r.width-16),0,1)*total()); });
// ---- trim a clip edge. PPS is FROZEN for the gesture, so the dragged edge tracks
// the cursor 1:1 (no rescale feedback). Neighbours slide via the CSS left transition. ----
function startTrim(e,el,side){ if(beatOn){ e.preventDefault(); $('#stat').textContent='locked — clip lengths are set by the beat'; return; }   // beat-cut mode: lengths locked to the beat
  e.preventDefault(); const before=snapClips(); const i=clipEls.indexOf(el);
  if(!selSet.has(i)) setSel(i);                                   // trimming an unselected clip selects just it
  // group = all selected (when multi-selected), else just this clip. The SAME source-second
  // delta is applied to every clip in the group, clamped per-clip — "shrink all by the same amount".
  const group=(selSet.size>1 && selSet.has(i)) ? [...selSet] : [i];
  dragging=true; const x0=e.clientX, P=PPS;
  const base=group.map(k=>({k, inn:clips[k].inn, out:clips[k].out}));   // baselines, so no drift
  function mv(ev){ const ds=(ev.clientX-x0)/P;
    for(const b of base){ const c=clips[b.k];
      if(side==='l') c.inn=clamp(b.inn+ds, 0, c.out-MIN);
      else           c.out=clamp(b.out+ds, c.inn+MIN, c.dur); }
    layout(); }
  function up(){ document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);
    dragging=false; layout(); if(group.includes(cur)) seekTo(curOutTime()); refreshPreload(); commitIfChanged(before); }
  document.addEventListener('mousemove',mv); document.addEventListener('mouseup',up);
}
// ---- reorder: the dragged clip follows the cursor (transition off); the others
// slide to their new slots (transition on). PPS frozen so nothing rescales mid-drag. ----
function startReorder(e,el){ e.preventDefault(); const before=snapClips(); const i=clipEls.indexOf(el); setSel(i);
  dragging=true; el.classList.add('dragging');
  const dragObj=clips[i], grabDX=e.clientX-el.getBoundingClientRect().left;
  function move(ev){
    const tr=track.getBoundingClientRect(), px=ev.clientX-tr.left-grabDX;
    el.style.left=px+'px';                               // dragged clip tracks the cursor
    const center=px+el.offsetWidth/2;                    // pick the slot its centre is over
    let acc=8, target=clips.length-1;
    for(let k=0;k<clips.length;k++){ const w=len(clips[k])*PPS; if(center<acc+w/2){target=k;break;} acc+=w; }
    const from=clips.indexOf(dragObj);
    if(target!==from){ clips.splice(from,1); clips.splice(target,0,dragObj);
      clipEls.splice(clipEls.indexOf(el),1); clipEls.splice(target,0,el);
      sel=target; selSet=new Set([target]); layout(el); }  // reflow neighbours, leave dragged where it is
  }
  function up(){ document.removeEventListener('mousemove',move);document.removeEventListener('mouseup',up);
    dragging=false; el.classList.remove('dragging'); setSel(clips.indexOf(dragObj));
    syncCur(); refreshPreload(); commitIfChanged(before); }   // snap into slot; re-sync cur + buffer to the new order
  document.addEventListener('mousemove',move); document.addEventListener('mouseup',up);
}
// ---- delete ----
$('#del').onclick=()=>{ if(selSet.size===0)return; pushHistory();
  const delIds=new Set([...selSet].map(k=>clips[k].id)), curId=cur>=0?clips[cur].id:null;
  const at=Math.min(...selSet);                                  // where to land the playhead after
  clips=clips.filter(c=>!delIds.has(c.id)); setSel(-1); drawTrack();
  if(!clips.length){ cur=-1; [v,vNext].forEach(x=>{x.classList.remove('show');x.removeAttribute('src');x.load();}); playing=false; $('#playbtn').textContent='▶'; }
  else if(curId===null || delIds.has(curId)){ loadClip(Math.min(at,clips.length-1),0,playing); }  // active clip was deleted
  else { syncCur(); refreshPreload(); } };
// ---- split / razor at the playhead: cut the active clip into two at the current
// source time (both halves share the source; right half inserted after). ----
function splitAtPlayhead(){ if(cur<0||cur>=clips.length)return;
  const c=clips[cur], srcT=v.currentTime;
  if(srcT<=c.inn+MIN || srcT>=c.out-MIN) return;     // too close to an edge to make two valid clips
  pushHistory();
  const right={...c, inn:srcT, out:c.out}; c.out=srcT;   // c keeps [inn,srcT]; right gets [srcT,out]
  clips.splice(cur+1,0,right); drawTrack(); setSel(cur); refreshPreload(); }
$('#split').onclick=splitAtPlayhead;
document.addEventListener('keydown',e=>{ const tag=(e.target.tagName||'').toLowerCase(), inField=(tag==='input'||tag==='textarea');
  if((e.metaKey||e.ctrlKey)&&(e.key==='z'||e.key==='Z')&&!inField){ e.preventDefault(); if(e.shiftKey)redo(); else undo(); return; }
  if(inField)return;
  if((e.key==='Delete'||e.key==='Backspace')&&selSet.size>0){e.preventDefault();$('#del').click();}
  else if(e.key===' '){e.preventDefault();$('#playbtn').click();}
  else if(e.key==='s'||e.key==='S'){e.preventDefault();splitAtPlayhead();}
  else if(e.key==='ArrowLeft'||e.key==='ArrowRight'){ e.preventDefault();   // frame-step (Shift = 0.5s coarse)
    if(playing){playing=false;v.pause();$('#playbtn').textContent='▶';}
    const fps=(cur>=0?clips[cur].fps:30)||30, step=e.shiftKey?0.5:1/fps;
    seekTo(clamp(curOutTime()+(e.key==='ArrowRight'?step:-step),0,total())); } });
$('#tzoom').oninput=e=>{ zoomF=+e.target.value; layout(); };
// ---- output aspect (1080p) ----
const aspect=()=>document.querySelector('#seg_ar .on').dataset.v;
function setAspect(v){ document.querySelectorAll('#seg_ar button').forEach(b=>b.classList.toggle('on',b.dataset.v===v)); }
document.querySelectorAll('#seg_ar button').forEach(b=>b.onclick=()=>setAspect(b.dataset.v));
function autoAspect(c){ if(!c)return; setAspect(c.w>c.h?'16:9':c.h>c.w?'9:16':'1:1'); }  // match first clip orientation
// ---- add clips (file upload) ----
$('#add').onclick=$('#add2').onclick=$('#add3').onclick=()=>$('#file').click();
$('#file').onchange=async e=>{ const files=[...e.target.files]; e.target.value=''; if(files.length)pushHistory();
  for(const f of files){ $('#stat').textContent='uploading '+f.name+'…';
    try{ const r=await fetch('/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f});
      const c=await r.json();
      if(c.error){ showErr(c.error); continue; }
      const wasEmpty=clips.length===0;
      clips.push({...c, inn:0, out:c.dur}); META[c.id]=c; drawTrack();
      if(wasEmpty){ autoAspect(c); loadClip(0,0,false); }   // first clip sets the default orientation
      else refreshPreload();                                // a newly-added clip may be the buffered "next"
    }catch(err){ showErr(''+err); } }
  $('#stat').textContent=clips.length+' clip'+(clips.length!==1?'s':'')+' loaded'; };
function showErr(m){ $('#errbox').innerHTML='<div class=err>'+m+'</div>'; setTimeout(()=>{$('#errbox').innerHTML='';},6000); }
// ---- export ----
const pring=$('#pring'), prfg=pring.querySelector('.rfg'), PRC=2*Math.PI*15.5;
prfg.style.strokeDasharray=PRC; const setRing=f=>{ prfg.style.strokeDashoffset=PRC*(1-clamp(f,0,1)); };
const dlFile=(url,name)=>{ const u=url+(url.includes('?')?'&':'?')+'dl='+encodeURIComponent(name);
  const a=document.createElement('a'); a.href=u; a.download=name; document.body.appendChild(a); a.click(); a.remove(); };
async function runSeqExport(gif){ if(!clips.length)return;
  const btn=gif?$('#gif'):$('#go'); $('#go').disabled=$('#gif').disabled=true;
  btn.textContent=gif?'GIF…':'Exporting…'; $('#errbox').innerHTML='';
  pring.style.display=''; setRing(0.02); $('#stat').textContent=gif?'rendering → gif':'rendering';
  const poll=setInterval(async()=>{ try{ const p=await(await fetch('/progress')).json();
    if(p.n>0){ setRing(p.i/p.n); $('#stat').textContent='rendering '+Math.round(100*p.i/p.n)+'%'; } }catch(e){} },300);
  try{ const r=await(await fetch('/export',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({aspect:aspect(),gif:!!gif,clips:clips.map(c=>({id:c.id,inn:c.inn,out:c.out}))})})).json();
    clearInterval(poll);
    if(r.error){ showErr(r.error); $('#stat').textContent='export failed'; }
    else if(gif){ if(r.gif){ setRing(1); dlFile(r.gif,r.gifName||'sequence.gif'); $('#stat').textContent='✓ '+(r.gifName||'gif'); }
      else{ showErr('GIF failed: '+(r.gifError||'unknown')); $('#stat').textContent='gif failed'; } }
    else{ setRing(1); dlFile('/download',r.name||'sequence.mp4'); $('#stat').textContent='✓ '+r.name; }
  }catch(e){ clearInterval(poll); showErr(''+e); $('#stat').textContent='export failed'; }
  setTimeout(()=>{pring.style.display='none';},600);
  $('#go').disabled=$('#gif').disabled=clips.length===0; $('#go').textContent='Export video'; $('#gif').textContent='GIF'; }
$('#go').onclick=()=>runSeqExport(false);
$('#gif').onclick=()=>runSeqExport(true);
$('#fcx').onclick=async()=>{ if(!clips.length)return;
  const b=$('#fcx'); b.disabled=true; b.textContent='Writing .fcpxml…'; $('#errbox').innerHTML='';
  try{ const r=await(await fetch('/fcpxml',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({clips:clips.map(c=>({id:c.id,inn:c.inn,out:c.out}))})})).json();
    if(r.error){ showErr(r.error); $('#stat').textContent='FCPXML failed'; }
    else{ dlFile(r.file,r.name||'sequence.fcpxml'); $('#stat').textContent='✓ '+r.name+' — import non-destructively'; }
  }catch(e){ showErr(''+e); } b.disabled=clips.length===0; b.textContent='Export FCPXML →'; };
// ---- beat-cut submode: derived from sequence mode. Type/tap a BPM, then cut
// every clip to the SAME beat-aligned length so each hard cut lands on the beat
// (a punchy Instagram beat-sync reel). ----
let beatOn=false, taps=[];
function setBeatMode(on){ beatOn=on;
  $('#beatmode').classList.toggle('on',on);
  $('#beatbar').classList.toggle('on',on);
  track.classList.toggle('beatlock',on);   // lock per-clip trim handles while beat-cut owns the lengths
  $('#crumb').innerHTML = on ? '<span class=sep>▸</span><span class=sub>beat-cut</span>' : '';
  $('#subtitle').textContent = on ? '· beat-synced cuts · for instagram' : '· multi-clip editor';
  if(on) updateBeatInfo(); }
$('#beatmode').onclick=()=>setBeatMode(!beatOn);
const beatsPerClip=()=>+document.querySelector('#seg_beats .on').dataset.v;
document.querySelectorAll('#seg_beats button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('#seg_beats button').forEach(x=>x.classList.toggle('on',x===b)); updateBeatInfo(); });
const curBPM=()=>clamp(+$('#bpm').value||120,20,400);
const beatLen=()=>beatsPerClip()*60/curBPM();
function updateBeatInfo(){ const L=beatLen();
  $('#beatinfo').textContent='→ '+L.toFixed(2)+'s/clip · '+clips.length+' clips = '+(L*clips.length).toFixed(1)+'s @ '+curBPM()+' BPM'; }
$('#bpm').oninput=updateBeatInfo;
// tap tempo: average the recent inter-tap intervals (gaps >3s reset)
$('#tap').onclick=()=>{ const now=performance.now(); if(taps.length&&now-taps[taps.length-1]>3000)taps=[];
  taps.push(now); $('#tap').classList.add('flash'); setTimeout(()=>$('#tap').classList.remove('flash'),90);
  if(taps.length>=2){ const iv=[]; for(let i=1;i<taps.length;i++)iv.push(taps[i]-taps[i-1]);
    const avg=iv.reduce((a,b)=>a+b,0)/iv.length; $('#bpm').value=clamp(Math.round(60000/avg),20,400); }
  $('#tapout').textContent=taps.length<2?'keep tapping…':(taps.length+' taps · '+$('#bpm').value+' BPM'); updateBeatInfo(); };
// cut every clip to exactly beatLen (all equal -> every boundary is on a beat)
$('#docut').onclick=()=>{ if(!clips.length)return; pushHistory(); const L=beatLen(); let short=0;
  clips.forEach(c=>{ if(c.dur<=L+0.001){ c.inn=0; c.out=c.dur; short++; }   // too short to fill a beat
    else { const inn=clamp(c.inn,0,c.dur-L); c.inn=inn; c.out=inn+L; } });
  layout(); if(cur>=0) seekTo(curOutTime()); refreshPreload(); updateBeatInfo();
  $('#stat').textContent='cut '+clips.length+' clips → '+L.toFixed(2)+'s @ '+curBPM()+' BPM'+(short?(' ('+short+' too short)'):''); };
// ---- boot ----
async function load(){ const A=await(await fetch('/analyze')).json();
  clips=(A.clips||[]).map(c=>{META[c.id]=c; return {...c, inn:0, out:c.dur};});
  drawTrack(); if(clips.length){ autoAspect(clips[0]); loadClip(0,0,false); } requestAnimationFrame(tick); }
load();
</script></body></html>"""

if __name__ == "__main__":
    seed(sys.argv[1:])
    port = free_port()
    print(f"sequence: http://127.0.0.1:{port}   ({len(CLIPS)} clip(s) seeded)", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
