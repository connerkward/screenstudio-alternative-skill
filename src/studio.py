#!/usr/bin/env python3
"""studio.py — local NLE-style editor for the eased-camera renderer.

LIVE preview: the zoom/spring camera is composited in the browser on a <canvas> in
real time (crop+scale from the playing source video), recomputed instantly as you drag
zoom blocks or tune the spring — like Screen Studio / an NLE. No render-to-preview.
"Export" renders the final high-quality 60fps file (render.py) for download.

Run:  python3 studio.py [recording.mp4]   (no arg -> synthetic fixture)
"""
import http.server, json, os, socket, subprocess, sys, threading, urllib.parse, importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(ROOT, ".studio-out"); os.makedirs(OUT, exist_ok=True)
# Exports write fixed paths under OUT (regions.json, speed.json, export.mp4,
# progress.json). The server is threaded, so two concurrent /render or /fcpxml
# requests (double-click, second tab, refresh mid-render) would interleave those
# writes and corrupt each other's output. Serialize all exports. (bug A)
RENDER_LOCK = threading.Lock()
FCPXML_LOCK = threading.Lock()       # FCPXML export writes its own file — must NOT share RENDER_LOCK,
                                     # else the instant FCPXML export blocks behind a slow video render
PRESET_LOCK = threading.Lock()       # serialize read-modify-write on the presets store
CALLOUT_LOCK = threading.Lock()      # serialize writes to the text-callout store
_s = importlib.util.spec_from_file_location("polish", os.path.join(_HERE, "polish.py"))
polish = importlib.util.module_from_spec(_s); _s.loader.exec_module(polish)
_fx = importlib.util.spec_from_file_location("fcpxml", os.path.join(_HERE, "fcpxml.py"))
fcpxml = importlib.util.module_from_spec(_fx); _fx.loader.exec_module(fcpxml)

def ensure_source(arg):
    if arg and os.path.exists(arg):
        v = os.path.abspath(arg); e = os.path.splitext(v)[0] + ".events.jsonl"
        return v, (e if os.path.exists(e) else None)
    v = os.path.join(ROOT, "test", "fixture.mp4"); e = os.path.join(ROOT, "test", "fixture.events.jsonl")
    if not os.path.exists(v):
        subprocess.run([sys.executable, os.path.join(ROOT, "test", "make-fixture.py"), v, e], check=True)
    return v, e

SRC_VIDEO, SRC_EVENTS = ensure_source(sys.argv[1] if len(sys.argv)>1 else None)
SAFE_DIRS = [os.path.dirname(SRC_VIDEO), OUT]

def analyze():
    vid = polish.probe(SRC_VIDEO)
    out = {"video":"/media?d=src&f="+urllib.parse.quote(os.path.basename(SRC_VIDEO)),
           "dur":vid["dur"], "w":vid["w"], "h":vid["h"], "fps":vid["fps"],
           "clicks":[], "idle":[], "regions":[]}
    if SRC_EVENTS:
        ev = polish.load_events(SRC_EVENTS, vid)
        out["clicks"] = [{"t":round(t,2),"x":round(x),"y":round(y)} for t,x,y in ev["clicks"]]
        regions = polish.zoom_regions(ev["clicks"], vid, 2.0, vid["dur"], keys=ev["keys"], moves=ev["moves"],
                                      click_bbox=ev.get("click_bbox"), key_bbox=ev.get("key_bbox"))
        idle = polish.intersect_spans(polish.idle_from_events(ev, vid["dur"]),
                                      polish.detect_freezes(SRC_VIDEO), vid["dur"])
        # a moment we zoom in to HIGHLIGHT must not also be fast-forwarded: carve zoom
        # spans out of idle, keep only what's left and still worth speeding (>=0.6s)
        idle = [(a,b) for a,b in polish.subtract_spans(idle, [(r["t0"],r["t1"]) for r in regions]) if b-a >= 0.6]
        out["idle"] = [{"t0":round(a,2),"t1":round(b,2)} for a,b in idle]
        out["regions"] = regions
        xs, ys = polish.smooth_positions(ev["moves"], vid["fps"], vid["dur"])  # crisp preview cursor
        step = max(1, int(vid["fps"]/20))                                      # ~20 Hz track
        out["cursor"] = [{"t":round(i/vid["fps"],3),"x":round(xs[i]),"y":round(ys[i])}
                         for i in range(0,len(xs),step)]
        # keystroke captions (source time): one window per burst, text accumulates via steps.
        # track uses t0/t1/text; the live preview grows the text via steps.
        out["keychips"] = [{"t0":round(g["t0"],2),"t1":round(g["t1"],2),"text":g["text"],
                            "steps":[[round(st,3),tx] for st,tx in g["steps"]]}
                           for g in polish.key_caps(ev["keys"])]
    return out

def render(p):
    regions = p.get("regions", [])
    feats = p.get("feats","").split(",")
    aspect = p.get("aspect","16:9")
    rj = os.path.join(OUT,"regions.json"); open(rj,"w").write(json.dumps(regions))
    out = os.path.join(OUT, "export.mp4")
    prog = os.path.join(OUT, "progress.json")
    try: os.remove(prog)
    except OSError: pass
    cmd = [sys.executable, os.path.join(_HERE, "render.py"), SRC_VIDEO, "--regions", rj,
           "--out", out, "--aspect", aspect, "--fit", str(p.get("fit","cover")), "--fps", "60",
           "--ramp", str(p.get("ramp",0.5)), "--curve", str(p.get("curve","smooth")), "--progress-file", prog]
    ti, to = float(p.get("trimIn",0) or 0), float(p.get("trimOut",0) or 0)   # source clip trim window
    if ti > 0: cmd += ["--trim-in", str(ti)]
    if to > 0: cmd += ["--trim-out", str(to)]
    if SRC_EVENTS: cmd += ["--events", SRC_EVENTS, "--cursor"]   # smooth cursor always on
    if "clickfx" in feats: cmd += ["--clickfx"]
    if "keys" in feats and SRC_EVENTS: cmd += ["--keys"]         # keystroke chips (needs events)
    co = [c for c in p.get("callouts", []) if (c.get("text") or "").strip()]
    if co:
        cj = os.path.join(OUT,"callouts.json"); open(cj,"w").write(json.dumps(co))
        cmd += ["--callouts", cj]
    if "speedup" in feats:
        ss = p.get("speedSegments")
        if ss:
            sj = os.path.join(OUT,"speed.json"); open(sj,"w").write(json.dumps(ss))
            cmd += ["--speed-segments", sj]
        else:
            cmd += ["--speedup","--idle-speed",str(p.get("idle",8))]
    if p.get("bg","none") != "none":
        cmd += ["--bg", str(p["bg"]), "--pad", str(p.get("pad",0.06)), "--radius", str(p.get("radius",18))]
        if not p.get("shadow",True): cmd += ["--no-shadow"]
    # NOTE: bg may be 'blur' (Screen-Studio-style blurred-source backdrop) — render.py handles it.
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0: return {"error": r.stderr[-1500:]}
    stem = os.path.splitext(os.path.basename(SRC_VIDEO))[0]
    res = {"video": "/media?d=out&f=export.mp4&v="+str(os.path.getmtime(out)),
           "name": stem+"-studio.mp4", "log": r.stdout.strip()}
    if p.get("gif"):                                  # also transcode the rendered mp4 -> GIF
        gif = os.path.join(OUT, "export.gif")
        try:
            polish.to_gif(out, gif)
            res["gif"] = "/download?f=export.gif&v="+str(os.path.getmtime(gif))
            res["gifName"] = stem+"-studio.gif"
        except Exception as e:
            res["gifError"] = str(e)
    return res

PRESETS_FILE = os.path.join(OUT, "presets.json")    # named style looks (bg/pad/aspect/…)
def get_presets():
    try: return json.load(open(PRESETS_FILE))
    except Exception: return {}
def save_preset(p):
    """{save:name, settings:{…}} writes a preset; {delete:name} removes one. Returns the
    full store. Caller holds PRESET_LOCK so the read-modify-write can't interleave."""
    d = get_presets()
    if p.get("delete"): d.pop(p["delete"], None)
    elif p.get("save"): d[str(p["save"])[:60]] = p.get("settings", {})
    tmp = PRESETS_FILE + ".tmp"; open(tmp,"w").write(json.dumps(d, indent=2)); os.replace(tmp, PRESETS_FILE)
    return d

CALLOUTS_FILE = os.path.join(OUT, "callouts.json")  # text callouts persisted server-side so
                                                    # manual placement survives reload AND is
                                                    # visible to a headless re-record (same server)
def get_callouts():
    try: return json.load(open(CALLOUTS_FILE))
    except Exception: return []
def save_callouts(p):
    """Replace the whole callout list ({callouts:[...]}) — the client owns the array and
    POSTs the full state (debounced) on every add/drag/edit/delete. Caller holds
    CALLOUT_LOCK so writes can't interleave."""
    cos = p.get("callouts", []) if isinstance(p, dict) else []
    tmp = CALLOUTS_FILE + ".tmp"; open(tmp,"w").write(json.dumps(cos, indent=2)); os.replace(tmp, CALLOUTS_FILE)
    return cos

# Session state (regions + speedSegs) persisted so a fresh page load restores the user's
# manual zoom/pan/crop edits — callouts already persist via callouts.json; this mirrors that
# store for the zoom/speed lanes so a re-opened tab shows the same reframes/pans to keep tweaking.
SESSION_FILE = os.path.join(OUT, "session.json")
SESSION_LOCK = threading.Lock()
def get_session():
    try: return json.load(open(SESSION_FILE))
    except Exception: return {}
def save_session(p):
    d = {"regions": p.get("regions", []), "speedSegs": p.get("speedSegs", [])} if isinstance(p, dict) else {}
    if isinstance(p, dict):
        if p.get("curve") is not None: d["curve"] = str(p.get("curve"))   # easing curve persists across restart
        if p.get("ramp")  is not None: d["ramp"]  = float(p.get("ramp"))
        if p.get("trimIn")  is not None: d["trimIn"]  = float(p.get("trimIn"))   # source clip trim persists
        if p.get("trimOut") is not None: d["trimOut"] = float(p.get("trimOut"))
    tmp = SESSION_FILE + ".tmp"; open(tmp,"w").write(json.dumps(d, indent=2)); os.replace(tmp, SESSION_FILE)
    return d

def export_fcpxml(p):
    """Non-destructive handoff: emit an FCPXML the user opens in Resolve/FCP/Premiere."""
    vid = polish.probe(SRC_VIDEO)
    feats = p.get("feats","").split(",")
    spec = {"src": os.path.abspath(SRC_VIDEO), "width": vid["w"], "height": vid["h"],
            "fps": vid["fps"], "duration": vid["dur"], "ramp": float(p.get("ramp",0.5)),
            "name": os.path.splitext(os.path.basename(SRC_VIDEO))[0],
            "regions": p.get("regions", []),
            "speed_segments": p.get("speedSegments", []) if "speedup" in feats else []}
    try:
        xml = fcpxml.to_fcpxml(spec)
    except Exception as e:
        return {"error": "FCPXML export failed: " + str(e)}
    op = os.path.join(OUT, "export.fcpxml"); open(op,"w").write(xml)
    stem = os.path.splitext(os.path.basename(SRC_VIDEO))[0]
    return {"file": "/download?f=export.fcpxml&v=" + str(os.path.getmtime(op)), "name": stem+"-studio.fcpxml"}

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # Chrome's <video> needs keep-alive/1.1 for progressive playback; 1.0 stalls it (every response sets Content-Length, so 1.1 is safe)
    def log_message(self,*a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body,bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type",ctype)
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def _q(self, u): return {k:v[0] for k,v in urllib.parse.parse_qs(u.query).items()}
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":         return self._send(200, HTML, "text/html; charset=utf-8")
        if u.path == "/favicon.svg": return self._send(200, FAVICON, "image/svg+xml")
        if u.path == "/favicon.ico": return self._send(204, b"", "image/svg+xml")
        if u.path == "/analyze":  return self._send(200, json.dumps(analyze()))
        if u.path == "/presets":  return self._send(200, json.dumps(get_presets()))
        if u.path == "/callouts": return self._send(200, json.dumps(get_callouts()))
        if u.path == "/session":  return self._send(200, json.dumps(get_session()))
        if u.path == "/progress":
            fp = os.path.join(OUT, "progress.json")
            try: return self._send(200, open(fp,"rb").read(), "application/json")
            except OSError: return self._send(200, b'{"i":0,"n":0}', "application/json")
        if u.path == "/click":
            cw = os.path.join(ROOT,"assets","click.wav")
            return self._send(200, open(cw,"rb").read() if os.path.isfile(cw) else b"", "audio/wav")
        if u.path == "/download":            # serve a generated file as an attachment (forces save)
            fn = os.path.basename(self._q(u).get("f",""))
            fp = os.path.join(OUT, fn)
            if not fn or not os.path.isfile(fp): return self._send(404, b"not found", "text/plain")
            ct = {"mp4":"video/mp4","fcpxml":"application/xml","xml":"application/xml","gif":"image/gif"}.get(fn.rsplit(".",1)[-1].lower(), "application/octet-stream")
            dl = os.path.basename(self._q(u).get("dl","")) or fn      # nicer save-as name
            self.send_response(200); self.send_header("Content-Type",ct)
            self.send_header("Content-Disposition",'attachment; filename="'+dl+'"')
            b = open(fp,"rb").read(); self.send_header("Content-Length",str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if u.path == "/media":
            q = self._q(u)
            base = OUT if q.get("d")=="out" else os.path.dirname(SRC_VIDEO)
            p = os.path.realpath(os.path.join(base, os.path.basename(q.get("f",""))))
            if not any(p.startswith(os.path.realpath(d)+os.sep) for d in SAFE_DIRS) or not os.path.isfile(p):
                return self._send(404, b"not found", "text/plain")
            return self._serve_media(p)
        self._send(404, b"404", "text/plain")
    def _serve_media(self, p):
        size = os.path.getsize(p); rng = self.headers.get("Range")
        with open(p,"rb") as f:
            if rng and rng.startswith("bytes="):
                s,_,e = rng[6:].partition("-")
                try:
                    start = int(s) if s else 0
                    end = min(int(e) if e else size-1, size-1)
                except ValueError:
                    start, end = 0, size-1               # malformed range -> serve whole
                if start > end or start >= size:         # unsatisfiable
                    self.send_response(416); self.send_header("Content-Range",f"bytes */{size}")
                    self.send_header("Content-Length","0"); self.end_headers(); return
                f.seek(start); data = f.read(end-start+1)
                self.send_response(206)
                self.send_header("Content-Type","video/mp4"); self.send_header("Accept-Ranges","bytes")
                self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
            else:
                data = f.read()
                self.send_response(200)
                self.send_header("Content-Type","video/mp4"); self.send_header("Accept-Ranges","bytes")
                self.send_header("Content-Length",str(size)); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # Guard the body parse: a malformed Content-Length or JSON body must
        # return a JSON 400 envelope, not raise out of the handler (which drops
        # the connection and surfaces as an opaque network error in the UI). (bug C)
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n).decode()
            payload = json.loads(body) if body.strip() else {}
        except (ValueError, UnicodeDecodeError) as e:
            return self._send(400, json.dumps({"error": "bad request: " + str(e)}))
        if u.path == "/render":
            with RENDER_LOCK: return self._send(200, json.dumps(render(payload)))     # bug A
        if u.path == "/fcpxml":
            with FCPXML_LOCK: return self._send(200, json.dumps(export_fcpxml(payload)))  # own lock → runs parallel to a render
        if u.path == "/presets":
            with PRESET_LOCK: return self._send(200, json.dumps(save_preset(payload)))
        if u.path == "/callouts":
            with CALLOUT_LOCK: return self._send(200, json.dumps(save_callouts(payload)))
        if u.path == "/session":
            with SESSION_LOCK: return self._send(200, json.dumps(save_session(payload)))
        self._send(404, b"404", "text/plain")

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1",0)); p=s.getsockname()[1]; s.close(); return p

FAVICON = (  # teal tile (matches the play button) + dark play glyph + diagonal zoom brackets
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
  '<rect x="1" y="1" width="30" height="30" rx="8" fill="#5fd0c0"/>'
  '<g fill="none" stroke="#06231f" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round">'
  '<path d="M8 12V8h4"/><path d="M24 20v4h-4"/></g>'
  '<path d="M13.5 10.5 22.5 16 13.5 21.5Z" fill="#06231f"/>'
  '</svg>'
)
HTML = r"""<!doctype html><html><head><meta charset=utf-8><title>studio</title>
<link rel=icon type="image/svg+xml" href="/favicon.svg">
<style>
:root{--bg:#0e0f13;--panel:#171922;--line:#262a36;--ink:#e7e9ef;--dim:#8a90a2;--acc:#5fd0c0;--zoom:#f2a65a;--idle:#3a3f50}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,Inter,system-ui,sans-serif;user-select:none;overflow:hidden}
/* single-screen app shell: topbar / (preview + controls rail) / timeline — no page scroll */
.app{height:100vh;display:flex;flex-direction:column;gap:10px;padding:12px 16px;max-width:1320px;margin:0 auto}
.topbar{display:flex;align-items:center;gap:10px;flex:none}
h1{font-size:16px;font-weight:650;letter-spacing:-.02em;margin:0;display:flex;align-items:center;gap:8px}h1 .muted{font-weight:400}
h1 .logo{width:20px;height:20px;display:block;border-radius:5px}
.tbtools{margin-left:auto;display:flex;align-items:center;gap:7px}
.tbtools .sep{width:1px;height:22px;background:var(--line);margin:0 3px}
.iconbtn{background:var(--panel);border:1px solid var(--line);color:var(--ink);border-radius:8px;width:32px;height:30px;font-size:15px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.iconbtn:disabled{opacity:.3;cursor:default}.iconbtn:not(:disabled):hover{border-color:var(--acc)}
.tbtn{background:var(--panel);border:1px solid var(--line);color:var(--ink);border-radius:8px;height:30px;padding:0 11px;font-size:12px;cursor:pointer}.tbtn:hover{border-color:var(--acc)}
.tbexp{background:var(--acc);color:#06231f;border:1px solid var(--acc);border-radius:8px;height:32px;padding:0 18px;font-size:13px;font-weight:700;cursor:pointer}.tbexp:disabled{opacity:.55;cursor:wait}
.tbstat{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
.ring{width:28px;height:28px;flex:none}
.ring .rbg{fill:none;stroke:var(--line);stroke-width:3.4}
.ring .rfg{fill:none;stroke:var(--acc);stroke-width:3.4;stroke-linecap:round;transform:rotate(-90deg);transform-origin:50% 50%;transition:stroke-dashoffset .18s linear}
/* main = preview monitor (fills) + controls rail */
.main{flex:1;display:flex;gap:12px;min-height:0}
.stage{flex:1;min-width:0;background:#07080b;border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;align-items:center;justify-content:center}
canvas{display:block;background:#000;max-width:100%;max-height:100%;width:auto;height:auto}
.side{flex:none;width:248px;overflow-y:auto;display:flex;flex-direction:column;gap:14px}
/* presentation mode (?present=1): hide the controls panel + export buttons so a demo recording of
   the editor is just the PREVIEW + TIMELINE — clean enough to read as a small looping GIF. */
.app.present .side{display:none}
.app.present .tbtools{display:none}
.grp>label{display:block;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);margin-bottom:5px}
.grp .mini{text-transform:none;letter-spacing:0;color:#5a6072;font-size:10px}
.sxbtn{width:100%;text-align:left;background:transparent;border:1px solid var(--line);color:var(--acc);border-radius:8px;padding:9px 11px;cursor:pointer;font-size:12px;font-weight:600}.sxbtn:hover{border-color:var(--acc)}.sxbtn:disabled{opacity:.55;cursor:wait}
.psel{width:100%;background:#10121a;border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:7px}
.prow{display:flex;gap:6px;margin-top:6px}
.pname{flex:1;min-width:0;background:#10121a;border:1px solid var(--line);color:var(--ink);border-radius:7px;padding:5px 8px;font-size:12px}
/* EDITOR = timeline at the bottom */
.editor{flex:none;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.transport{display:flex;align-items:center;gap:10px;margin-bottom:9px}
.play{background:var(--acc);color:#06231f;border:0;border-radius:8px;width:40px;height:34px;font-size:15px;cursor:pointer;font-weight:700}
.time{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums;min-width:188px;white-space:nowrap}
.addz{background:#10121a;border:1px solid var(--line);color:var(--acc);border-radius:8px;height:30px;padding:0 13px;cursor:pointer;font-size:12px;font-weight:600}.addz:hover{border-color:var(--acc)}
.hint{color:var(--dim);font-size:11px;margin-left:auto;text-align:right}
.frmeta{color:#6f7689;font-variant-numeric:tabular-nums;font-size:10px}
/* trim = grey-out the excluded ends of the FULL ruler. The ruler ALWAYS shows the full
   source; trimming never reflows. .trimGrey bands sit at the OUTPUT-time positions of
   [0,S2O(trimIn)] (left) and [S2O(trimOut),outDur] (right); .trimHandle markers ride the
   inner edge of each band (at S2O(trimIn)/S2O(trimOut)), draggable to set in/out. */
.trimGrey{position:absolute;top:0;height:100%;background:rgba(8,9,13,0.62);z-index:5;pointer-events:none;border-radius:6px}
.trimHandle{position:absolute;top:0;height:100%;width:8px;background:#5fd0c0;cursor:ew-resize;z-index:6;opacity:.85;transform:translateX(-50%)}
.trimHandle:hover{opacity:1;width:9px}
.trimHandle::after{content:"";position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:2px;height:46%;background:#06231f;border-radius:2px}
.trimHandle.l{border-radius:7px 0 0 7px}.trimHandle.r{border-radius:0 7px 7px 0}
.edrow{display:flex;gap:9px;align-items:flex-start}
.tlscroll{flex:1;min-width:0;overflow-x:auto}
.tzoom{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:13px}
.tzoom input{width:88px;accent-color:var(--acc)}
.gutter{flex:none;width:52px;display:flex;flex-direction:column;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:#7f879b;padding-top:1px}
.gutter span{display:flex;align-items:center}
.gutter span:nth-child(1){height:42px}.gutter span:nth-child(2){height:30px}.gutter span:nth-child(3){height:28px}.gutter span:nth-child(4){height:24px}.gutter span:nth-child(5){height:16px}
.tl{position:relative;width:100%;min-width:440px;height:140px;background:#0c0e15;border:1px solid var(--line);border-radius:8px;cursor:crosshair;overflow:hidden}
.lane{position:absolute;left:0;right:0;pointer-events:none}
.laneZ{top:0;height:42px;background:#12141d}
.laneS{top:42px;height:30px;background:#0d0f16;border-top:1px solid #1c202b;border-bottom:1px solid #1c202b}
.laneCO{top:72px;height:28px;background:#10121a}
.laneK{top:100px;height:24px;background:#0d0f16;border-top:1px solid #1c202b;border-bottom:1px solid #1c202b}
.laneClk{top:124px;height:16px;background:#10121a}
.zoomblock{position:absolute;top:5px;height:32px;background:linear-gradient(var(--zoom),#d98b3e);border-radius:7px;cursor:grab;box-shadow:0 2px 6px #0007;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#241500;overflow:hidden;z-index:3}
.calloutblock{position:absolute;top:75px;height:22px;background:linear-gradient(#6aa9e0,#4d86c4);border-radius:6px;cursor:grab;box-shadow:0 2px 6px #0006;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#06182e;overflow:hidden;z-index:3;padding:0 4px}
.calloutblock span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;pointer-events:none}
.calloutblock.sel{outline:2px solid #fff;outline-offset:1px;box-shadow:0 0 0 4px #6aa9e033,0 2px 8px #0008}
.calloutblock .h{position:absolute;top:0;width:10px;height:100%;cursor:ew-resize;pointer-events:auto;z-index:4}
.calloutblock .h.l{left:0}.calloutblock .h.r{right:0}
.calloutblock:hover{filter:brightness(1.09)}
.keyblock{position:absolute;top:103px;height:18px;background:#2d3344;border:1px solid #3f4760;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#c7cde0;overflow:hidden;z-index:2;padding:0 4px;pointer-events:none}
.keyblock span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.keyblock.off{opacity:.38}   /* detected but won't render in the export (checkbox off) */
.zoomblock.sel{outline:2px solid #fff;outline-offset:1px;box-shadow:0 0 0 4px #5fd0c033,0 2px 8px #0008}
.zoomblock .h,.idleb .h{position:absolute;top:0;width:11px;height:100%;cursor:ew-resize;pointer-events:auto;z-index:4}
.zoomblock .h::after,.idleb .h::after{content:"";position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:2px;height:44%;border-radius:2px;background:#ffffff96}
.zoomblock .h.l,.idleb .h.l{left:0}.zoomblock .h.r,.idleb .h.r{right:0}
.zoomblock:hover,.idleb:hover{filter:brightness(1.09)}                       /* signal interactivity */
.zoomblock:hover .h::after,.idleb:hover .h::after{background:#fff;height:60%;width:3px}  /* trim grips pop on hover */
.click{position:absolute;top:126px;width:2px;height:12px;margin-left:-1px;border-radius:1px;background:#6a7488;pointer-events:none;z-index:2}
.idleb{position:absolute;top:46px;height:22px;background:repeating-linear-gradient(45deg,#2a2e3c,#2a2e3c 7px,#222633 7px,#222633 14px);border:1px solid #3a4357;border-radius:5px;cursor:grab;overflow:visible;display:flex;align-items:center;justify-content:center;z-index:2}
.tlend{position:absolute;top:0;height:100%;background:rgba(6,7,11,0.55);border-left:1px solid #2a2e3c;pointer-events:none;z-index:1}
.idleb span{font-size:10px;color:#cfd3df;font-weight:700;letter-spacing:.03em;white-space:nowrap;pointer-events:none}
.ph{position:absolute;top:0;width:2px;height:100%;background:var(--acc);pointer-events:none;z-index:5;box-shadow:0 0 6px #5fd0c0aa}
.idleb.sel{outline:2px solid var(--acc);outline-offset:1px;box-shadow:0 0 0 4px #5fd0c033}
.rubber{position:absolute;background:rgba(95,208,192,0.12);border:1px solid rgba(95,208,192,0.65);border-radius:3px;pointer-events:none;z-index:6}
/* Inspector = property panel DOCKED to the right of the timeline (always present).
   Height is LOCKED to the timeline height (140px) with internal scroll, so switching
   between the empty "Select a clip" state and a filled clip body never changes the row
   geometry — the #tl bounding box must not move when a block is selected. (bug: tl jump) */
.insp{flex:none;width:212px;height:140px;background:#13151c;border:1px solid var(--line);border-radius:9px;padding:10px 12px;display:flex;flex-direction:column;overflow:auto}
.insp .ibody{display:none;flex-direction:column;gap:7px;min-height:100%}
.insp.kind-zoom .ibodyNum,.insp.kind-speed .ibodyNum{display:flex}
.insp.kind-co .ibodyCo{display:flex}
.insp.kind-multi .ibodyMulti{display:flex}
.insp.kind-multi .dot{background:#cfd3df}
.insp.has-sel .inspempty{display:none}
.inspempty{margin:auto;text-align:center;color:var(--dim);font-size:12px;line-height:1.6}
.insp .ihead{display:flex;align-items:center;gap:8px}
.insp .ihead .dot{width:9px;height:9px;border-radius:3px;flex:none}
.insp.kind-speed .dot{background:#7c8398}.insp.kind-zoom .dot{background:var(--zoom)}.insp.kind-co .dot{background:#6aa9e0}
.insp .iinput{background:#10121a;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:6px 8px;font-size:13px;width:100%}
.insp .iinput:focus{outline:none;border-color:var(--acc)}
.insp .ilbl{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim)}
.insp .anchgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
.insp .anchgrid button{aspect-ratio:1;background:#10121a;border:1px solid var(--line);border-radius:5px;cursor:pointer;padding:0;position:relative}
.insp .anchgrid button::after{content:"";position:absolute;inset:0;margin:auto;width:5px;height:5px;border-radius:50%;background:#5a6072}
.insp .anchgrid button:hover{border-color:#6aa9e0}
.insp .anchgrid button.on{border-color:#6aa9e0;background:#13202b}.insp .anchgrid button.on::after{background:#6aa9e0}
.insp .corow2{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:6px;align-items:center}
.insp .corow2 input{background:#10121a;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:4px 6px;font-size:12px;width:100%;min-width:0;-moz-appearance:textfield}
.insp .corow2 input::-webkit-outer-spin-button,.insp .corow2 input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.insp .ititle{font-size:12px;font-weight:700;color:var(--ink)}
.insp .isub{font-size:10px;color:var(--dim)}
.insp .proprow{display:flex;align-items:center;justify-content:space-between;margin-top:1px}
.insp .plabel{font-size:12px;color:var(--dim)}
.insp .num{display:flex;align-items:center;font-variant-numeric:tabular-nums}
.insp .num input[type=number]{width:50px;background:#10121a;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:3px 6px;font-size:13px;font-weight:700;text-align:right;-moz-appearance:textfield}
.insp .num input[type=number]::-webkit-outer-spin-button,.insp .num input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.insp .num .unit{font-size:11px;color:var(--dim);font-weight:700;margin-left:3px}
.insp input[type=range]{width:100%;accent-color:var(--acc)}
.insp .panrow{display:flex;gap:6px;align-items:center;margin-top:4px}
.insp .panrow .seg{margin-top:0;flex:1}
.insp .panrow .seg button{padding:4px;font-size:11px}
.insp .panbtn{flex:none;background:#13202b;border:1px solid #2f4a44;color:var(--acc);border-radius:7px;padding:4px 9px;cursor:pointer;font-size:11px;font-weight:700;white-space:nowrap}
.insp .panbtn:hover{border-color:var(--acc)}
.insp .panbtn.on{background:#2a1518;border-color:#4a2530;color:#ff9a9a}
.insp .panhint{font-size:10px;color:#5a6072;line-height:1.35}
.insp:not(.kind-zoom) .panrow,.insp:not(.kind-zoom) .panhint{display:none}
.insp.kind-zoom.no-pan #seg_kf{display:none}     /* no end keyframe yet -> no Start/End toggle */
.insp .del{margin-top:auto;background:#2a1518;border:1px solid #4a2530;color:#ff9a9a;border-radius:7px;padding:6px;cursor:pointer;font-size:12px;width:100%}
/* secondary controls — quieter than the editor above */
.row{display:flex;gap:14px;margin-top:14px;flex-wrap:wrap}
.ctl{background:#13151c;border:1px solid var(--line);border-radius:12px;padding:14px 16px;flex:1;min-width:240px}
.ctl h3{margin:0 0 11px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
label.f{display:flex;align-items:center;gap:9px;padding:5px 0;cursor:pointer}label.f input{accent-color:var(--acc);width:16px;height:16px}
.sl{margin:11px 0 3px}.sl label{display:flex;justify-content:space-between;color:var(--dim);font-size:12px}.sl input{width:100%;accent-color:var(--acc)}
.seg{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}.seg button{flex:1;background:#10121a;border:1px solid var(--line);color:var(--dim);border-radius:7px;padding:6px;cursor:pointer;font-size:12px}.seg button.on{color:var(--ink);border-color:var(--acc)}
/* Export buttons: primary = filled accent (renders mp4); secondary = outline (FCPXML handoff) */
button.exp{border-radius:10px;padding:11px 12px;cursor:pointer;width:100%;text-align:center;line-height:1.25;display:block}
button.exp .lab{font-weight:700;font-size:14px;display:block}
button.exp .slab{font-size:11px;font-weight:500;opacity:.78;display:block;margin-top:1px}
button.exp:disabled{opacity:.5;cursor:wait}
button.exp.primary{background:var(--acc);color:#06231f;border:1px solid var(--acc);margin-top:14px}
button.exp.secondary{background:transparent;color:var(--acc);border:1px solid var(--line);margin-top:9px}
button.exp.secondary .slab{color:var(--dim);opacity:1}
.dlrow{margin-top:10px;display:none}.dlrow.show{display:block}a.dl{color:var(--acc);font-size:13px;margin-right:16px;text-decoration:none}
.err{color:#ff8080;font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap;background:#1a1115;border:1px solid #4a2530;border-radius:8px;padding:10px;margin-top:10px}
.muted{color:var(--dim);font-size:12px}
/* Progressive disclosure: advanced controls collapsed by default */
details.adv{margin-top:12px;border-top:1px solid var(--line);padding-top:4px}
details.adv>summary{list-style:none;cursor:pointer;color:var(--dim);font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding:8px 0 4px;display:flex;align-items:center;gap:7px;user-select:none}
details.adv>summary::-webkit-details-marker{display:none}
details.adv>summary .chev{transition:transform .15s;font-size:10px}
details.adv[open]>summary .chev{transform:rotate(90deg)}
details.adv>summary:hover{color:var(--ink)}
#errbox{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:60;max-width:92vw}
@media(max-width:820px),(max-height:560px){ html,body{height:auto;overflow:auto} .app{height:auto;min-height:100vh} .main{flex-direction:column} .stage{min-height:240px} .side{width:100%} .tlwrap{overflow-x:auto} .hint{display:none} }
</style></head><body><div class=app>
<div class=topbar>
  <h1><img class=logo src="/favicon.svg" alt=""> studio <span class=muted>· screen-recording editor</span></h1>
  <div class=tbtools>
    <button class=tbtn id=reset title="Restore the auto-detected zooms & speed-ups">↺ Reset to auto</button>
    <button class=iconbtn id=undo title="Undo (⌘Z)" disabled>↶</button>
    <button class=iconbtn id=redo title="Redo (⌘⇧Z)" disabled>↷</button>
    <span class=sep></span>
    <button class=tbtn id=gif title="Export an animated GIF (palette, 18fps)">GIF</button>
    <button class=tbexp id=go>Export video</button>
    <svg id=pring class=ring viewBox="0 0 36 36" style=display:none><circle class=rbg cx=18 cy=18 r=15.5></circle><circle class=rfg cx=18 cy=18 r=15.5></circle></svg>
    <span class=tbstat id=stat></span>
  </div>
</div>
<div class=main>
  <div class=stage><canvas id=pv></canvas></div>
  <aside class=side>
    <div class=grp><label>Preset <span class=mini>· save your look</span></label>
      <select id=presetSel class=psel><option value="">— preset —</option></select>
      <div class=prow><input id=presetName placeholder="name" class=pname>
        <button class=tbtn id=presetSave>Save</button>
        <button class=tbtn id=presetDel title="Delete selected preset">✕</button></div></div>
    <div class=grp><label>Aspect</label>
      <div class=seg id=seg_ar><button data-v=16:9 class=on>16:9</button><button data-v=1:1>1:1</button><button data-v=9:16>9:16</button></div></div>
    <div class=grp><label>Fit <span class=mini>· when source ≠ aspect</span></label>
      <div class=seg id=seg_fit><button data-v=cover class=on>fill</button><button data-v=contain>fit</button></div></div>
    <div class=grp><label>Background <span class=mini>· live</span></label>
      <div class=seg id=seg_bg><button data-v=none class=on>none</button><button data-v=blur>blur</button><button data-v=dusk>dusk</button><button data-v=ocean>ocean</button><button data-v=warm>warm</button><button data-v=mint>mint</button><button data-v=slate>slate</button><button data-v=dark>dark</button><button data-v=light>light</button></div></div>
    <details class=adv id=keyDetails><summary><span class=chev>▶</span> Keystroke chips</summary>
      <div class=mini style=margin:6px 0>Typed text shown as background-aware chips, auto-detected from your recorded keys and laid out on the <b>Keys</b> track. Untick to leave them out of the export.</div>
      <label class=f><input type=checkbox id=f_keys checked> Render keystroke chips</label></details>
    <details class=adv><summary><span class=chev>▶</span> Text callouts</summary>
      <div class=mini style=margin:6px 0>Add a label to the <b>Text</b> track — then select it to edit in the inspector. <b>＋ Text</b> below, or double-click the track.</div></details>
    <details class=adv><summary><span class=chev>▶</span> Frame styling</summary>
      <div class=sl style=margin-top:6px><label>Padding <b id=l_pad>6%</b></label><input type=range id=s_pad min=0 max=0.14 step=0.01 value=0.06></div>
      <div class=sl><label>Corner radius <b id=l_radius>18 px</b></label><input type=range id=s_radius min=0 max=44 step=2 value=18></div>
      <label class=f style=margin-top:6px><input type=checkbox id=f_shadow checked> Drop shadow</label></details>
    <details class=adv><summary><span class=chev>▶</span> Advanced</summary>
      <div style=margin-top:6px><label>Easing curve</label>
        <div class=seg id=seg_ease><button data-v=smooth class=on>Smooth</button><button data-v=snappy>Snappy</button><button data-v=linear>Linear</button></div></div>
      <div class=sl style=margin-top:6px><label>Smoothness (ramp) <b id=l_ramp>0.50 s</b></label><input type=range id=s_ramp min=0.2 max=0.9 step=0.05 value=0.5></div>
      <div class=sl><label>Default zoom (new) <b id=l_zoom>2.0×</b></label><input type=range id=s_zoom min=1.2 max=2.8 step=0.1 value=2.0></div>
      <label class=f style=margin-top:8px><input type=checkbox id=f_speedup checked> Speed up idle time</label>
      <label class=f><input type=checkbox id=f_clickfx checked> Click effects (ripple + sound)</label></details>
    <div class=grp style=margin-top:auto>
      <button class=sxbtn id=fcx>Export for editing (FCPXML) →</button>
      <div class=mini style=margin-top:5px>opens in Resolve / Final Cut / Premiere — non-destructive</div>
      <div class=mini id=fcstat style=margin-top:3px></div></div>
  </aside>
</div>
<div class=editor>
  <div class=transport>
    <button class=play id=playbtn>▶</button>
    <span class=time id=time>0:00 / 0:00</span>
    <button class=addz id=addz title="Add a zoom at the playhead">＋ Zoom</button>
    <button class=addz id=addt title="Add a text callout at the playhead">＋ Text</button>
    <label class=tzoom title="Zoom the timeline">⌕<input type=range id=tzoom min=1 max=8 step=0.5 value=1></label>
    <span class=hint id=hint></span>
  </div>
  <div class=edrow>
    <div class=gutter><span>Zoom</span><span>Speed</span><span>Text</span><span>Keys</span><span>Clicks</span></div>
    <div class=tlscroll>
      <div class=tl id=tl>
        <div class="lane laneZ"></div><div class="lane laneS"></div><div class="lane laneCO"></div><div class="lane laneK"></div><div class="lane laneClk"></div>
        <div class=ph id=ph style=left:0></div>
      </div>
    </div>
    <div class=insp id=insp>
      <div class=inspempty id=inspEmpty>Select a clip<br><span class=mini>to edit zoom, speed, or text</span></div>
      <div class="ibody ibodyMulti">
        <div class=ihead><span class=dot></span><span class=ititle id=inspMultiN>0 selected</span></div>
        <div class=isub>⌘/ctrl-click to toggle · shift-click for a range · drag empty space to box-select</div>
        <button class=del id=inspMultiDel>Delete selected</button>
      </div>
      <div class="ibody ibodyNum">
        <div class=ihead><span class=dot></span><span class=ititle id=inspTitle></span></div>
        <div class=isub id=inspSub></div>
        <div class=proprow><span class=plabel id=inspPropLabel>Speed</span><span class=num><input type=number id=inspNum><span class=unit id=inspUnit>×</span></span></div>
        <input type=range id=inspSlider>
        <div class=panrow id=panRow>
          <div class=seg id=seg_kf><button data-kf=start class=on>Start</button><button data-kf=end>End</button></div>
          <button class=panbtn id=panToggle title="Add a second keyframe so the camera moves (pans) across this clip">＋ pan</button>
        </div>
        <div class=panhint id=panHint>Drag the crop rect on the preview to reframe.</div>
        <button class=del id=inspDel>Delete clip</button>
      </div>
      <div class="ibody ibodyCo">
        <div class=ihead><span class=dot></span><span class=ititle>Text callout</span></div>
        <div class=isub id=coSub></div>
        <input id=coText class=iinput placeholder="label text…">
        <div class=ilbl>Position</div>
        <div class=anchgrid id=coAnch></div>
        <div class=corow2><span class=plabel>In</span><input type=number step=0.1 min=0 id=coIn><span class=plabel>Out</span><input type=number step=0.1 min=0 id=coOut></div>
        <button class=del id=coDel>Delete callout</button>
      </div>
    </div>
  </div>
</div>
</div>
<video id=v style=display:none muted loop playsinline></video>
<div id=errbox></div>
</div><script>
const $=s=>document.querySelector(s), v=$('#v'), pv=$('#pv'), ctx=pv.getContext('2d',{willReadFrequently:true}),
      tl=$('#tl'), ph=$('#ph'), TWO=Math.PI*2;
let A=null, regions=[], speedSegs=[], sel=-1, selSpd=-1, selCo=-1, dur=0, W=0, Hh=0, FPS=60, N=0, zA=[],xA=[],yA=[];   // match the 30fps source so ←/→ steps one REAL frame
// Source clip trim (in/out) in SOURCE seconds: the demo only uses [trimIn, trimOut]. The
// time-remap (speedMap/S2O/O2S) clips its table to this window, so outDur, the ruler, the
// playhead range, and playback all honor the trim. Camera arrays (zA/xA/yA) stay full-source
// (indexed by source frame), so trimming never disturbs zoom/pan/easing. trimOut<=0 → end.
let trimIn=0, trimOut=0;
const trimHi=()=>(trimOut>trimIn?trimOut:dur);   // effective out-point (0 sentinel = source end)
// Multi-select: a SET of indices within ONE lane. selLane names that lane ('zoom'|'speed'|'co');
// selMulti holds the indices. The single sel/selSpd/selCo mirrors are kept in sync — when exactly
// one block is selected they point at it (so the single-clip inspector + drag handlers still work);
// when >1 the inspector shows an "N selected" summary. lastClick tracks the anchor for shift-range.
let selLane=null, selMulti=[], lastClick=-1;
let kfEdit='start';   // which keyframe of a panning zoom clip the crop-rect drag edits ('start'|'end')
// a region is a PAN when it carries an end keyframe (cx1/cy1 present)
function isPan(r){ return r && r.cx1!==undefined && r.cx1!==null && r.cx1!==''; }
function laneArr(lane){ return lane==='zoom'?regions:lane==='speed'?speedSegs:lane==='co'?COUTS:[]; }
function clearSel(){ sel=selSpd=selCo=-1; selLane=null; selMulti=[]; lastClick=-1; }
// reflect selMulti/selLane back into the single sel/selSpd/selCo mirrors used everywhere else
function syncSel(){ sel=selSpd=selCo=-1;
  const one = selMulti.length===1 ? selMulti[0] : -1;   // single mirror only when exactly one
  if(selLane==='zoom') sel=one; else if(selLane==='speed') selSpd=one; else if(selLane==='co') selCo=one; }
// SINGLE-select a block in a lane (plain click + drag handlers + programmatic select)
function selectOne(lane,i){ selLane=lane; selMulti=[i]; lastClick=i; syncSel(); }
// cmd/ctrl-click: toggle a block in/out of the set (only within the active lane)
function toggleSel(lane,i){ if(selLane!==lane){ selectOne(lane,i); return; }
  const k=selMulti.indexOf(i);
  if(k>=0){ selMulti.splice(k,1); if(!selMulti.length){ clearSel(); return; } }
  else selMulti.push(i);
  lastClick=i; selMulti.sort((a,b)=>a-b); syncSel(); }
// shift-click: range-select from the anchor (lastClick) to i, within the active lane
function rangeSel(lane,i){ if(selLane!==lane || lastClick<0){ selectOne(lane,i); return; }
  const a=Math.min(lastClick,i), b=Math.max(lastClick,i), s=[];
  for(let k=a;k<=b;k++) s.push(k); selMulti=s; syncSel(); }
// route a block click through the right selection mode given the modifier keys
function clickSelect(lane,i,ev){ if(ev&&(ev.metaKey||ev.ctrlKey)) toggleSel(lane,i);
  else if(ev&&ev.shiftKey) rangeSel(lane,i); else selectOne(lane,i); }
let COUTS=[];  // text callouts [{text,t0,t1,anchor,size}] in OUTPUT time
let _coSaveT=null;
function saveCallouts(){ clearTimeout(_coSaveT); _coSaveT=setTimeout(()=>{   // debounced persist to server
  fetch('/callouts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({callouts:COUTS})}).catch(()=>{}); },350); }
let _seshSaveT=null;
function saveSession(){ clearTimeout(_seshSaveT); _seshSaveT=setTimeout(()=>{   // debounced persist of regions+speed+trim (manual edits survive reload)
  fetch('/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({regions,speedSegs,curve:easeMode(),ramp:+$('#s_ramp').value,trimIn,trimOut})}).catch(()=>{}); },350); }
let coBounds={};   // per-frame drawn pill rects {i:{x,y,W,H}} (canvas px) for preview hit-testing
// Crop-rect overlay geometry, recomputed each frame for the selected zoom clip, in CANVAS px.
// cropOv.content = the displayed-video rect [x,y,w,h]; .start/.end = the framed-viewport rect
// for each keyframe. Used to draw the draggable reframe overlay AND to hit-test pv mouse drags.
let cropOv=null;
// Map a keyframe (cx,cy,z) of a region to its crop rect within the content rect C=[x,y,w,h].
// The viewport the camera shows is a sub-rectangle of the SOURCE of size (W/z)x(Hh/z'),
// matching tick()'s crop math (output-aspect crop, clamped inside the source), centered at
// (cx,cy). We express it as a fraction of the source, then map onto the displayed content rect.
function cropRectFor(cx,cy,z,C){
  const [tw,th]=AR[aspect()], ca=tw/th; z=Math.max(1,z);
  let cw=W/z, ch=cw/ca; if(ch>Hh){ch=Hh;cw=ch*ca;} if(cw>W){cw=W;ch=cw/ca;}
  let x0=Math.max(0,Math.min(W-cw,cx-cw/2)), y0=Math.max(0,Math.min(Hh-ch,cy-ch/2));
  return {x:C.x+(x0/W)*C.w, y:C.y+(y0/Hh)*C.h, w:(cw/W)*C.w, h:(ch/Hh)*C.h};
}
const ANCH=['top-left','top','top-right','left','center','right','bottom-left','bottom','bottom-right'];
let clickBuf=null;
let actx=null, lastT=-1;
function playTick(){ if(!actx||!clickBuf)return;
  const s=actx.createBufferSource(), g=actx.createGain(); s.buffer=clickBuf; g.gain.value=0.85;
  s.connect(g); g.connect(actx.destination); s.start(actx.currentTime); }
function easeCurve(a, curve){
  // KEEP IN SYNC with render.py ease_curve(). Full ease-in-out: ~0 velocity at BOTH
  // ends, peak mid-move (no constant-velocity middle). smooth=smootherstep (default),
  // snappy=steeper midslope, linear=constant velocity (old mechanical feel).
  if(a<=0)return 0; if(a>=1)return 1;
  if(curve==='linear')return a;
  let s=a*a*a*(a*(a*6-15)+10);                 // smootherstep 6a^5-15a^4+10a^3
  if(curve==='snappy')s=s*s*(3-2*s);           // smoothstep on top → snappier
  return s;
}
function easePan(a){
  // KEEP IN SYNC with render.py ease_pan(). Human-camera-operator pan: asymmetric,
  // ease-OUT-dominant — quick pickup, EARLY velocity peak (~a=0.27), then a long gentle
  // deceleration that settles into the final framing. Velocity exactly 0 at both ends (no
  // hard start/stop, no bounce). Used for cx/cy translation only (zoom z keeps easeCurve).
  // smootherstep evaluated on a front-loaded input warp a**0.65 — the warp moves the
  // velocity peak early & lengthens the settle tail while preserving the 0-velocity ends.
  if(a<=0)return 0; if(a>=1)return 1;
  const w=Math.pow(a,0.65);
  return w*w*w*(w*(w*6-15)+10);                // smootherstep(a**0.65)
}
function easeTraj(key, rest){
  // PORTED from render.py ease_traj — MATCHES export 1:1. Full ease-in-out (smootherstep
  // by default) on every move: zoom rest->v, the in-region PAN v->v1, and v1->rest, so the
  // pan reads cinematic (slow start, smooth middle, gentle stop), not a linear slide.
  const arr=new Array(N).fill(rest), ramp=+$('#s_ramp').value, curve=easeMode();
  const kv=r=>key==='z'?+r.z:(key==='cx'?+r.cx:+r.cy);
  const kv1=r=>{ const x=r[key+'1']; return (x!==undefined&&x!==null&&x!=='')?+x:kv(r); };
  for(const r of [...regions].sort((a,b)=>a.t0-b.t0)){
    const o0=r.t0,o1=r.t1,v=kv(r),v1=kv1(r),rmp=Math.min(ramp,(o1-o0)/2);
    const isPan=(key!=='z'&&v1!==v);                     // cx/cy with a pan target → ONE continuous glide across the WHOLE region (overlaps the zoom in/out → no hard stop, no beat)
    for(let i=0;i<N;i++){const t=i/FPS; if(t<o0||t>o1)continue;
      if(isPan){const a=(o1>o0)?(t-o0)/(o1-o0):1;arr[i]=v+(v1-v)*(curve==='linear'?a:easePan(a));}            // continuous pan start->end (human ease-out-dominant feel; z keeps easeCurve)
      else if(rmp>0&&t<o0+rmp){const a=(t-o0)/rmp,b=(key==='z'?rest:v);arr[i]=b+(v-b)*easeCurve(a,curve);}    // z zoom-in (static cx/cy: hold)
      else if(rmp>0&&t>o1-rmp){const a=(o1-t)/rmp,b=(key==='z'?rest:v1);arr[i]=b+(v1-b)*easeCurve(a,curve);}  // z zoom-out
      else arr[i]=v;}
  }
  return arr;
}
function easeMode(){ const el=document.querySelector('#seg_ease .on'); return el?el.dataset.v:'smooth'; }
function recompute(){ if(!dur) return; N=Math.round(dur*FPS)+1;
  zA=easeTraj('z',1); xA=easeTraj('cx',W/2); yA=easeTraj('cy',Hh/2); }
function fmt(t){t=t||0;return Math.floor(t/60)+':'+String(Math.floor(t%60)).padStart(2,'0');}
// NLE frame helpers — OUTPUT frame = round(outputSeconds * FPS); exact, integer.
function oFrame(o){ return Math.round((o||0)*FPS); }
function totalOutFrames(M){ return Math.round((M.outDur||0)*FPS); }
// MM:SS:FF timecode for an OUTPUT-second value (FF = frame within the current second)
function tc(o){ o=o||0; const tf=oFrame(o), f=tf%FPS, s=Math.floor(tf/FPS);
  return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')+':'+String(f).padStart(2,'0'); }
// source seconds -> source frame number (for inspector metadata)
function sFrame(t){ return Math.round((t||0)*FPS); }
const AR={'16:9':[1920,1080],'1:1':[1080,1080],'9:16':[1080,1920]};
const BG={dark:['#14161e','#0c0d12'],light:['#eef0f4','#d6dbe4'],dusk:['#311c48','#182856'],
  ocean:['#0d223a','#0b4e62'],warm:['#402c1c','#68382a'],mint:['#12342e','#1a4e42'],slate:['#282c38','#161921']};
const aspect=()=>document.querySelector('#seg_ar .on').dataset.v;
const bgsel=()=>document.querySelector('#seg_bg .on').dataset.v;
function frame(){ return {bg:bgsel(), pad:+$('#s_pad').value, radius:+$('#s_radius').value, shadow:$('#f_shadow').checked}; }
function rr(ctx,x,y,w,h,r){ ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath(); }
// draw one screen-fixed, background-aware label pill — mirrors render.py _callout_render/_callout_pos
function drawCalloutLabel(text,anchor,sizeFrac,alpha){ if(!text)return;
  const Tw=pv.width,Th=pv.height,px=Math.max(12,sizeFrac*Th),padx=px*0.62,pady=px*0.42;
  ctx.save(); ctx.font='600 '+px+'px -apple-system,Inter,system-ui,sans-serif'; ctx.textBaseline='middle';
  const W=Math.ceil(ctx.measureText(text).width+2*padx),H=Math.ceil(px+2*pady),m=Math.round(0.045*Th),
        a=(anchor||'bottom').toLowerCase(),
        hz=a.includes('left')?'left':a.includes('right')?'right':'center',
        vt=a.includes('top')?'top':a.includes('bottom')?'bottom':'middle';
  let x=hz==='left'?m:hz==='right'?Tw-W-m:(Tw-W)/2, y=vt==='top'?m:vt==='bottom'?Th-H-m:(Th-H)/2;
  x=Math.max(0,Math.min(x,Tw-W)); y=Math.max(0,Math.min(y,Th-H));
  let lum=128; try{ const d=ctx.getImageData(x|0,y|0,Math.max(1,W),Math.max(1,H)).data; let s=0;
    for(let p=0;p<d.length;p+=4) s+=(d[p]+d[p+1]+d[p+2])/3; lum=s/(d.length/4); }catch(e){}
  const darkBg=lum<=128;                               // dark bg -> light pill, dark text
  ctx.globalAlpha=alpha; rr(ctx,x,y,W,H,H*0.42);
  ctx.fillStyle=darkBg?'rgba(244,244,247,0.85)':'rgba(18,18,22,0.80)'; ctx.fill();
  ctx.fillStyle=darkBg?'#101014':'#fff'; ctx.fillText(text,x+padx,y+H/2);
  ctx.restore(); return {x,y,W,H}; }
// chrome for the selected callout in the preview: accent outline + bottom-right resize grip
function drawCoChrome(b){ ctx.save(); ctx.strokeStyle='#6aa9e0'; ctx.lineWidth=Math.max(1.5,pv.width*0.0016);
  rr(ctx,b.x-3,b.y-3,b.W+6,b.H+6,8); ctx.stroke();
  const g=Math.max(12,pv.width*0.014); ctx.fillStyle='#6aa9e0'; rr(ctx,b.x+b.W-g,b.y+b.H-g,g,g,3); ctx.fill();
  ctx.restore(); }
// ---- crop-rect reframe overlay (drawn on the preview for the selected zoom clip) ----
// Solid teal rect = start framed viewport; dashed amber rect = end (only if a pan); an arrow
// from start-center to end-center = the pan path. A corner handle on the EDITED keyframe's rect
// resizes it (changes z). The whole overlay is dimmed when the clip isn't the active edit target.
function CHANDLE(){ return Math.max(11,pv.width*0.013); }
function drawCropOverlay(){
  if(sel<0 || !cropOv) return; const C=cropOv.content;
  const pan=cropOv.end!=null;
  const drawRect=(R,col,dashed,active)=>{ ctx.save();
    ctx.lineWidth=Math.max(2,pv.width*0.0022)*(active?1.15:1);
    ctx.setLineDash(dashed?[Math.max(8,pv.width*0.012),Math.max(6,pv.width*0.009)]:[]);
    ctx.strokeStyle=col; ctx.globalAlpha=active?1:0.7; rr(ctx,R.x,R.y,R.w,R.h,Math.max(4,pv.width*0.006)); ctx.stroke();
    if(active){ ctx.setLineDash([]); ctx.fillStyle=col;            // bottom-right resize handle (=z)
      const g=CHANDLE(); rr(ctx,R.x+R.w-g,R.y+R.h-g,g,g,3); ctx.fill(); }
    ctx.restore(); };
  const sActive=!pan||kfEdit==='start', eActive=pan&&kfEdit==='end';
  if(pan){                                                          // arrow start-center -> end-center
    const a=cropOv.start,b=cropOv.end, ax=a.x+a.w/2,ay=a.y+a.h/2,bx=b.x+b.w/2,by=b.y+b.h/2;
    ctx.save(); ctx.strokeStyle='#ffd27a'; ctx.fillStyle='#ffd27a'; ctx.lineWidth=Math.max(2,pv.width*0.0026);
    ctx.beginPath(); ctx.moveTo(ax,ay); ctx.lineTo(bx,by); ctx.stroke();
    const ang=Math.atan2(by-ay,bx-ax), hl=Math.max(10,pv.width*0.016);
    ctx.beginPath(); ctx.moveTo(bx,by);
    ctx.lineTo(bx-hl*Math.cos(ang-0.4),by-hl*Math.sin(ang-0.4));
    ctx.lineTo(bx-hl*Math.cos(ang+0.4),by-hl*Math.sin(ang+0.4)); ctx.closePath(); ctx.fill(); ctx.restore();
  }
  drawRect(cropOv.start, '#5fd0c0', false, sActive);                // start = solid teal
  if(pan) drawRect(cropOv.end, '#f2a65a', true, eActive);           // end   = dashed amber
  // legend so the human can read the overlay (per label-overlays-rule)
  ctx.save(); const fs=Math.max(11,pv.width*0.012); ctx.font='700 '+fs+'px -apple-system,Inter,sans-serif';
  ctx.textBaseline='top'; const lx=C.x+6, ly=C.y+6;
  ctx.globalAlpha=0.92; ctx.fillStyle='rgba(8,9,13,0.62)';
  const txt=pan?'■ start   ▦ end   → pan':'■ framed viewport — drag to reframe';
  const tw=ctx.measureText(txt).width; rr(ctx,lx-4,ly-3,tw+8,fs+8,5); ctx.fill();
  ctx.fillStyle='#e7e9ef'; ctx.fillText(txt,lx,ly+1); ctx.restore();
}
// NLE-style timeline (FCP retime / Premiere rate-stretch):
// - The ruler is FIXED: full bar width = source duration. Edits never rescale it,
//   so upstream content is always planted.
// - Blocks render at OUTPUT-time positions on that ruler. Speeding an idle region
//   shrinks ITS block; everything downstream slides left (ripple); empty track
//   grows at the right end, exactly like an NLE sequence getting shorter.
// - Idle regions' SOURCE RANGE is locked (detected footage never changes). Only
//   their SPEED is editable: select -> inspector slider, or drag the block's
//   right edge = rate-stretch.
function speedMap(){
  const on=$('#f_speedup').checked, segs=on?[...speedSegs].sort((a,b)=>a.t0-b.t0):[];
  // The remap ALWAYS spans the FULL source [0,dur]; trimming never clips the table, so the
  // ruler/timeline always represents the whole source and no regions/speed/callouts are dropped.
  // The trim is purely a greyed-out overlay + a playback loop window (see drawTrimHandles/tick).
  const lo=0, hi=Math.max(0.01,dur||1);
  const table=[]; let cur=lo,out=0;
  for(const s of segs){ let t0=Math.max(cur,s.t0),t1=Math.min(hi,s.t1),sp=Math.max(1,s.speed);  // clamp segs into the window
    if(t1-t0<0.01)continue;
    if(t0>cur){table.push({s0:cur,s1:t0,sp:1,o0:out});out+=t0-cur;}
    table.push({s0:t0,s1:t1,sp,o0:out});out+=(t1-t0)/sp;cur=t1; }
  if(cur<hi){table.push({s0:cur,s1:hi,sp:1,o0:out});out+=hi-cur;}
  if(!table.length){table.push({s0:lo,s1:hi,sp:1,o0:0});out=hi-lo;}
  return {table,outDur:out};
}
function S2O(t,M){ for(const e of M.table){ if(t<=e.s1+1e-6) return e.o0+(Math.max(e.s0,Math.min(t,e.s1))-e.s0)/e.sp; } return M.outDur; }
function O2S(o,M){ for(const e of M.table){ const eo=e.o0+(e.s1-e.s0)/e.sp; if(o<=eo+1e-6) return e.s0+(o-e.o0)*e.sp; } return dur; }
function localSpeed(t,M){ for(const e of M.table){ if(t<=e.s1+1e-6) return e.sp; } return 1; }
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
let curOD=1; const oPct=o=>(o/(curOD||1)*100);   // ruler spans the OUTPUT duration -> content fills width at zoom 1
function setCanvas(){ const [tw,th]=AR[aspect()]; pv.width=tw; pv.height=th; }
function tick(){
  if(v.readyState>=2 && N){
    // Keep playback inside the trimmed source window [trimIn, trimHi]: wrap to trimIn at the
    // out-point (so it loops the trimmed range), and pull the playhead in if it drifts before in.
    { const lo=Math.max(0,trimIn), hi=Math.min(trimHi(),dur||trimHi());
      if(v.currentTime>hi-0.001){ v.currentTime=lo; }
      else if(v.currentTime<lo-0.001){ v.currentTime=lo; } }
    const t=v.currentTime||0, i=Math.max(0,Math.min(N-1,Math.round(t*FPS)));
    const M=speedMap(), psp=clamp(localSpeed(t,M),0.0625,16);  // play EDITED pacing live (browser caps rate at 16)
    curOD=M.outDur;
    if(Math.abs(v.playbackRate-psp)>1e-3) v.playbackRate=psp;
    let z=Math.max(1,zA[i]||1), cx=xA[i]||W/2, cy=yA[i]||Hh/2;
    const [tw,th]=AR[aspect()], ca=tw/th;           // crop to OUTPUT aspect, then zoom
    let cw=W/z, ch=cw/ca; if(ch>Hh){ch=Hh;cw=ch*ca;} if(cw>W){cw=W;ch=cw/ca;}
    let x0=Math.max(0,Math.min(W-cw,cx-cw/2)), y0=Math.max(0,Math.min(Hh-ch,cy-ch/2));
    const fr=frame(); let dOX=0,dOY=0,dW=pv.width,dH=pv.height, framed=fr.bg!=='none';
    if(framed){
      if(fr.bg==='blur'){
        // Screen-Studio blurred-source backdrop: draw the current frame scaled-to-cover,
        // blurred, behind the padded content, then a slight dark overlay for contrast.
        const cov=Math.max(pv.width/W, pv.height/Hh), bw=W*cov, bh=Hh*cov;
        const bl=Math.max(8,Math.round(pv.width/26));
        ctx.save(); ctx.filter='blur('+bl+'px)';
        try{ctx.drawImage(v, 0,0,W,Hh, (pv.width-bw)/2,(pv.height-bh)/2,bw,bh);}catch(e){}
        ctx.restore();
        ctx.fillStyle='rgba(8,9,13,0.30)'; ctx.fillRect(0,0,pv.width,pv.height);
      } else {
        const g=ctx.createLinearGradient(0,0,0,pv.height); g.addColorStop(0,BG[fr.bg][0]);g.addColorStop(1,BG[fr.bg][1]);
        ctx.fillStyle=g; ctx.fillRect(0,0,pv.width,pv.height);
      }
      dW=pv.width*(1-2*fr.pad); dH=pv.height*(1-2*fr.pad); dOX=(pv.width-dW)/2; dOY=(pv.height-dH)/2;
      const rad=fr.radius*pv.width/1280;
      ctx.save(); if(fr.shadow){ctx.shadowColor='rgba(0,0,0,0.5)';ctx.shadowBlur=24*pv.width/1280;ctx.shadowOffsetY=12*pv.width/1280;}
      rr(ctx,dOX,dOY,dW,dH,rad); ctx.fillStyle='#000'; ctx.fill(); ctx.restore();
      ctx.save(); rr(ctx,dOX,dOY,dW,dH,rad); ctx.clip(); }
    try{ctx.drawImage(v, x0,y0,cw,ch, dOX,dOY,dW,dH);}catch(e){}
    const sx=dW/cw, k=pv.width/1280;   // map source px -> content rect, fixed-size fx
    if(A.clicks && $('#f_clickfx').checked) for(const c of A.clicks){ const p=(t-c.t)/0.5; if(p<0||p>1)continue;
      const rx=dOX+(c.x-x0)*sx, ry=dOY+(c.y-y0)*sx, r=(9+p*46)*k, al=1-p;
      let col='255,255,255'; try{ const px=ctx.getImageData(Math.max(0,Math.min(pv.width-1,rx|0)),Math.max(0,Math.min(pv.height-1,ry|0)),1,1).data;
        if((px[0]+px[1]+px[2])/3>128) col='30,30,34'; }catch(e){}
      ctx.beginPath();ctx.arc(rx,ry,r,0,6.2832);
      ctx.fillStyle='rgba('+col+','+(0.16*al)+')';ctx.fill();
      ctx.lineWidth=3*k;ctx.strokeStyle='rgba('+col+','+(0.85*al)+')';ctx.stroke(); }
    const cp=cursorAt(t); if(cp){ drawCursor(ctx,dOX+(cp[0]-x0)*sx,dOY+(cp[1]-y0)*sx,k); }
    if(framed) ctx.restore();
    // crop-rect reframe overlay geometry for the selected zoom clip (canvas px)
    if(sel>=0 && regions[sel]){ const r=regions[sel], C={x:dOX,y:dOY,w:dW,h:dH};
      cropOv={content:C, start:cropRectFor(+r.cx,+r.cy,+r.z,C),
        end: isPan(r)?cropRectFor(+r.cx1,(r.cy1!==undefined&&r.cy1!==''?+r.cy1:+r.cy),(r.z1!==undefined&&r.z1!==''?+r.z1:+r.z),C):null}; }
    else cropOv=null;
    // screen-fixed labels — WYSIWYG with the render: text callouts (output time) + keystroke chips (source time)
    const ot=S2O(t,M); coBounds={};
    for(let ci=0;ci<COUTS.length;ci++){ const c=COUTS[ci],t0=+c.t0,t1=+c.t1, inr=ot>=t0&&ot<=t1;
      if(!inr && ci!==selCo) continue;                         // selected callout stays visible (ghosted) for editing
      let fade=inr?Math.max(0,Math.min(1,(ot-t0)/0.3,(t1-ot)/0.3)):0.55;
      if(inr && fade<=0 && ci!==selCo) continue;
      coBounds[ci]=drawCalloutLabel((c.text||'').trim()||'label', c.anchor, +c.size||0.036, Math.max(fade,ci===selCo?0.55:fade)); }
    if(selCo>=0 && coBounds[selCo]) drawCoChrome(coBounds[selCo]);   // selection outline + resize grip
    if($('#f_keys').checked && A.keychips) for(const kc of A.keychips){ if(t<kc.t0||t>kc.t1)continue;
      const fade=Math.max(0,Math.min(1,(t-kc.t0)/0.3,(kc.t1-t)/0.3)); if(fade<=0)continue;
      let txt=kc.text; if(kc.steps&&kc.steps.length){ txt=kc.steps[0][1]; for(const s of kc.steps){ if(s[0]<=t) txt=s[1]; else break; } }
      drawCalloutLabel(txt,'bottom',0.055,fade); }   // single window, text accumulates char-by-char
    // FAST-FORWARD badge — a fixed pill in the preview while an idle span is being sped up, so the
    // compression reads as a deliberate feature (not a glitch). Drawn over the content, top-left.
    if(psp>=2.4){   // badge only for a real fast-forward finale, not a gentle opening trim
      const k2=pv.width/1280, bx=dOX+18*k2, by=dOY+16*k2, h=32*k2, lbl=(Math.round(psp*10)/10)+'×';
      ctx.save(); ctx.font='800 '+(16*k2)+'px ui-sans-serif,system-ui';
      const ts=9*k2, tw=ctx.measureText(lbl).width, padx=12*k2, w=ts*2.2+padx*0.7+tw+padx*1.6;
      ctx.fillStyle='rgba(18,18,22,0.84)'; rr(ctx,bx,by,w,h,h/2); ctx.fill();
      const ty=by+h/2, tx=bx+padx; ctx.fillStyle='#6fe3ad';
      for(let s=0;s<2;s++){ const ox=tx+s*ts*1.1; ctx.beginPath(); ctx.moveTo(ox,ty-ts); ctx.lineTo(ox+ts,ty); ctx.lineTo(ox,ty+ts); ctx.closePath(); ctx.fill(); }
      ctx.fillStyle='#fff'; ctx.textBaseline='middle'; ctx.textAlign='left'; ctx.fillText(lbl, tx+ts*2.2+padx*0.7, ty); ctx.restore();
    }
    if(!v.paused && $('#f_clickfx').checked && A.clicks){
      if(t<lastT) lastT=t;
      for(const c of A.clicks) if(c.t>lastT && c.t<=t) playTick();
      lastT=t;
    } else lastT=t;
    drawCropOverlay();                              // reframe/pan overlay on top of everything
    // OUTPUT-time playhead on the fixed ruler. While paused-stepping/scrubbing, drive it from the
    // COMMANDED frame (_stepTF) so the playhead glides smoothly with the readout even when the heavy
    // decode lags behind (the picture catches up; the playhead never stalls on the in-flight seek).
    const _of=(v.paused && _stepTF!=null)?_stepTF:oFrame(S2O(t,M)), _ot=_of/FPS;
    ph.style.left=oPct((v.paused && _stepTF!=null)?(_stepTF/FPS):S2O(t,M))+'%';
    $('#time').textContent=fmt(_ot)+' / '+fmt(M.outDur)+'  ·  f'+_of+' / '+totalOutFrames(M);
  }
  requestAnimationFrame(tick);
}
function cursorAt(t){ const C=A.cursor; if(!C||!C.length)return null;
  let lo=0,hi=C.length-1; while(lo<hi){const m=(lo+hi)>>1; if(C[m].t<t)lo=m+1;else hi=m;}
  const b=C[Math.max(1,lo)],a=C[Math.max(0,lo-1)]; const d=b.t-a.t||1,f=Math.max(0,Math.min(1,(t-a.t)/d));
  return [a.x+(b.x-a.x)*f, a.y+(b.y-a.y)*f]; }
function drawCursor(ctx,x,y,k){
  const pts=[[0,0],[0,0.80],[0.23,0.62],[0.35,0.93],[0.47,0.88],[0.33,0.59],[0.58,0.59]], s=40*k;
  ctx.save();ctx.translate(x,y);ctx.beginPath();
  pts.forEach((p,i)=>{const X=p[0]*s,Y=p[1]*s; i?ctx.lineTo(X,Y):ctx.moveTo(X,Y);});ctx.closePath();
  ctx.shadowColor='rgba(0,0,0,0.45)';ctx.shadowBlur=3.5*k;ctx.shadowOffsetX=0.8*k;ctx.shadowOffsetY=1.2*k;
  ctx.lineJoin='round';ctx.lineWidth=1.7*k;ctx.strokeStyle='#fff';ctx.stroke();
  ctx.shadowColor='transparent';ctx.fillStyle='#161618';ctx.fill();ctx.restore(); }
// is block i of `lane` in the current selection set? (highlights ALL selected blocks)
function isSel(lane,i){ return selLane===lane && selMulti.includes(i); }
function drawBlocks(){
  [...tl.querySelectorAll('.zoomblock,.click,.idleb,.tlend,.calloutblock,.keyblock')].forEach(e=>e.remove());
  const M=speedMap(); curOD=M.outDur;              // ruler = output duration -> content fills the width
  if($('#f_speedup').checked) speedSegs.forEach((s,si)=>{const e=document.createElement('div');e.className='idleb'+(isSel('speed',si)?' sel':'');
    e.style.left=oPct(S2O(s.t0,M))+'%';e.style.width=(oPct(S2O(s.t1,M))-oPct(S2O(s.t0,M)))+'%';e.style.pointerEvents='auto';
    e.innerHTML='<div class="h l" title="trim"></div><span>⏩ '+(+s.speed).toFixed(0)+'×</span><div class="h r" title="trim"></div>';
    e.onmousedown=ev=>startSpeedDrag(ev,si,'move');                                        // body = select + move
    e.querySelector('.h.l').onmousedown=ev=>{ev.stopPropagation();startSpeedDrag(ev,si,'l');};  // edge = trim
    e.querySelector('.h.r').onmousedown=ev=>{ev.stopPropagation();startSpeedDrag(ev,si,'r');};  // edge = trim
    tl.appendChild(e);});
  A.clicks.forEach(c=>{const d=document.createElement('div');d.className='click';d.title='click';d.style.left=oPct(S2O(c.t,M))+'%';tl.appendChild(d);});
  regions.forEach((r,i)=>{const b=document.createElement('div');b.className='zoomblock'+(isSel('zoom',i)?' sel':'');
    b.style.left=oPct(S2O(r.t0,M))+'%';b.style.width=(oPct(S2O(r.t1,M))-oPct(S2O(r.t0,M)))+'%';
    b.innerHTML='<div class="h l"></div>'+(+r.z).toFixed(1)+'×<div class="h r"></div>';
    b.onmousedown=e=>startDrag(e,i,'move');
    b.querySelector('.h.l').onmousedown=e=>{e.stopPropagation();startDrag(e,i,'l');};
    b.querySelector('.h.r').onmousedown=e=>{e.stopPropagation();startDrag(e,i,'r');};
    b.onwheel=e=>{e.preventDefault();selectOne('zoom',i);const rr=regions[i];   // scroll = zoom level
      rr.z=Math.round(clamp(rr.z+(e.deltaY<0?0.1:-0.1),1.2,2.8)*10)/10;drawBlocks();recompute();showInsp();schedule();};
    b.ondblclick=e=>{e.stopPropagation();regions.splice(i,1);clearSel();drawBlocks();recompute();showInsp();commit();};
    tl.appendChild(b);});
  // keystroke chips — read-only track, ALWAYS populated from detected keys (like clicks);
  // the checkbox toggles whether they render in the export, dimming the lane when off
  if(A.keychips){ const koff=!$('#f_keys').checked;
    A.keychips.forEach(kc=>{const b=document.createElement('div');b.className='keyblock'+(koff?' off':'');
    const l=oPct(S2O(kc.t0,M)); b.style.left=l+'%'; b.style.width=Math.max(0.6,oPct(S2O(kc.t1,M))-l)+'%';
    b.innerHTML='<span>⌨ '+(kc.text||'').replace(/</g,'&lt;')+'</span>'; tl.appendChild(b);}); }
  // text callouts — interactive track (output time; drag to retime, content edited in side list)
  COUTS.forEach((c,i)=>{const b=document.createElement('div');b.className='calloutblock'+(isSel('co',i)?' sel':'');
    const o0=clamp(+c.t0||0,0,curOD),o1=clamp(+c.t1||0,0,curOD),l=oPct(o0);
    b.style.left=l+'%'; b.style.width=Math.max(0.6,oPct(o1)-l)+'%';
    b.innerHTML='<div class="h l"></div><span>'+((c.text||'').trim()?(c.text).replace(/</g,'&lt;'):'⬚ label')+'</span><div class="h r"></div>';
    b.onmousedown=e=>startCoDrag(e,i,'move');
    b.querySelector('.h.l').onmousedown=e=>{e.stopPropagation();startCoDrag(e,i,'l');};
    b.querySelector('.h.r').onmousedown=e=>{e.stopPropagation();startCoDrag(e,i,'r');};
    tl.appendChild(b);});
  $('#hint').textContent=regions.length+' zoom · '+speedSegs.length+' speed · '+COUTS.length+' text · '+A.clicks.length+' clicks';
  drawTrimHandles();
}
function showInsp(){ const insp=$('#insp'), s=$('#inspSlider'), num=$('#inspNum');
  insp.classList.remove('has-sel','kind-zoom','kind-speed','kind-co','kind-multi');
  if(selMulti.length>1){                                 // multiple blocks selected -> minimal summary
    insp.classList.add('has-sel','kind-multi');
    const kind=selLane==='zoom'?'zoom':selLane==='speed'?'speed':'text';
    $('#inspMultiN').textContent=selMulti.length+' '+kind+' clips selected';
    return; }
  if(sel>=0){ insp.classList.add('has-sel','kind-zoom');
    const r=regions[sel], len=(r.t1-r.t0), pan=isPan(r);
    insp.classList.toggle('no-pan',!pan);
    if(!pan) kfEdit='start';                                   // no end keyframe -> always editing start
    const editEnd=pan&&kfEdit==='end';
    $('#inspTitle').textContent=pan?'Pan clip':'Zoom clip';
    { const M=speedMap();   // show OUTPUT-frame range (what the export sees) + SOURCE frames
      $('#inspSub').innerHTML=fmt(r.t0)+'–'+fmt(r.t1)+'  ·  '+len.toFixed(1)+'s'+(pan?'  ·  pan':'')
        +'<br><span class=frmeta>out f'+oFrame(S2O(r.t0,M))+'–'+oFrame(S2O(r.t1,M))
        +'  ·  src f'+sFrame(r.t0)+'–'+sFrame(r.t1)+'</span>'; }
    $('#inspPropLabel').textContent=pan?(editEnd?'End zoom':'Start zoom'):'Zoom';
    const zv=editEnd?((r.z1!==undefined&&r.z1!=='')?+r.z1:+r.z):+r.z;
    s.min=1;s.max=2.8;s.step=0.1;s.value=zv;
    num.min=1;num.max=2.8;num.step=0.1;num.value=(+zv).toFixed(1);
    $('#inspUnit').textContent='×';
    $('#panToggle').textContent=pan?'– pan':'＋ pan'; $('#panToggle').classList.toggle('on',pan);
    [...$('#seg_kf').children].forEach(b=>b.classList.toggle('on',b.dataset.kf===kfEdit));
    $('#panHint').textContent=(pan?('Drag the '+(editEnd?'dashed END':'solid START')+' rect to reframe; corner = zoom; arrow = path.')
                                  :'Drag the crop rect on the preview to reframe.')
                              +' Drag a clip edge on the timeline to shorten it.'; }
  else if(selSpd>=0){ insp.classList.add('has-sel','kind-speed');
    const sp=speedSegs[selSpd], len=(sp.t1-sp.t0);
    $('#inspTitle').textContent='Idle clip (sped up)';
    { const M=speedMap();
      $('#inspSub').innerHTML=len.toFixed(1)+'s source → '+(len/Math.max(1,sp.speed)).toFixed(1)+'s output'
        +'<br><span class=frmeta>out f'+oFrame(S2O(sp.t0,M))+'–'+oFrame(S2O(sp.t1,M))
        +'  ·  src f'+sFrame(sp.t0)+'–'+sFrame(sp.t1)+'</span>'; }
    $('#inspPropLabel').textContent='Speed';
    s.min=2;s.max=16;s.step=1;s.value=sp.speed;
    num.min=2;num.max=16;num.step=1;num.value=(+sp.speed).toFixed(0);
    $('#inspUnit').textContent='×'; }
  else if(selCo>=0){ insp.classList.add('has-sel','kind-co');
    const c=COUTS[selCo];
    $('#coText').value=c.text||''; $('#coIn').value=(+c.t0).toFixed(1); $('#coOut').value=(+c.t1).toFixed(1);
    $('#coSub').innerHTML=(+c.t0).toFixed(1)+'–'+(+c.t1).toFixed(1)+'s output  ·  bg-aware'
      +'<br><span class=frmeta>in f'+oFrame(+c.t0)+'  ·  out f'+oFrame(+c.t1)+'  (output frames)</span>';
    [...$('#coAnch').children].forEach(b=>b.classList.toggle('on', b.dataset.a===(c.anchor||'bottom'))); } }
// keep slider + number in sync, write back to the selected clip
function inspSet(v){
  if(sel>=0){ const r=regions[sel], editEnd=isPan(r)&&kfEdit==='end';
    if(editEnd) r.z1=v; else r.z=v;                            // write start z or end z1
    $('#inspSlider').value=v; $('#inspNum').value=v.toFixed(1);
    drawBlocks(); recompute(); }
  else if(selSpd>=0){ const sp=speedSegs[selSpd]; sp.speed=v; $('#inspSlider').value=v; $('#inspNum').value=v.toFixed(0);
    $('#inspSub').textContent=(sp.t1-sp.t0).toFixed(1)+'s source → '+((sp.t1-sp.t0)/Math.max(1,v)).toFixed(1)+'s output';
    drawBlocks(); } schedule(); }
$('#inspSlider').oninput=e=>inspSet(+e.target.value);
$('#inspNum').oninput=e=>{ let v=+e.target.value; if(isNaN(v))return;
  const lo=+e.target.min,hi=+e.target.max; v=clamp(v,lo,hi); inspSet(v); };
$('#inspNum').onchange=e=>{ let v=clamp(+e.target.value||+e.target.min,+e.target.min,+e.target.max);
  e.target.value = (selSpd>=0)?v.toFixed(0):v.toFixed(1); inspSet(v); };
// delete ALL selected blocks in the active lane. Splice high->low so earlier indices
// stay valid as we remove, then commit/persist exactly as the single delete did.
function delSel(){ if(!selLane||!selMulti.length) return;
  const arr=laneArr(selLane), idx=[...selMulti].sort((a,b)=>b-a);   // high -> low
  for(const i of idx){ if(i>=0&&i<arr.length) arr.splice(i,1); }
  const lane=selLane; clearSel(); drawBlocks(); showInsp();
  if(lane==='co'){ saveCallouts(); } else { recompute(); commit(); } }
$('#inspDel').onclick=delSel;
$('#inspMultiDel').onclick=delSel;
// ---- pan keyframes: ＋pan adds an END keyframe (cx1/cy1/z1) = a two-keyframe camera move;
// –pan removes it (back to a static zoom). The Start/End toggle picks which keyframe the
// crop-rect drag + inspector slider edit. ----
$('#panToggle').onclick=()=>{ if(sel<0)return; const r=regions[sel];
  if(isPan(r)){ delete r.cx1; delete r.cy1; delete r.z1; kfEdit='start'; }   // remove end keyframe
  else { r.cx1=+r.cx; r.cy1=+r.cy; r.z1=+r.z; kfEdit='end'; }                // seed end == start, edit it
  recompute(); drawBlocks(); showInsp(); commit(); };
[...$('#seg_kf').children].forEach(b=>b.onclick=()=>{ if(sel<0)return; kfEdit=b.dataset.kf;
  [...$('#seg_kf').children].forEach(x=>x.classList.toggle('on',x===b)); showInsp(); });
// ---- text-callout inspector controls (selected callout edited HERE, not in the side panel) ----
(function(){ const g=$('#coAnch'); ANCH.forEach(a=>{ const b=document.createElement('button'); b.dataset.a=a; b.title=a;
  b.onclick=()=>{ if(selCo<0)return; COUTS[selCo].anchor=a; showInsp(); saveCallouts(); }; g.appendChild(b); }); })();
$('#coText').oninput=e=>{ if(selCo<0)return; COUTS[selCo].text=e.target.value; drawBlocks(); saveCallouts(); };  // preview updates via tick
$('#coIn').oninput=e=>{ if(selCo<0)return; COUTS[selCo].t0=+e.target.value; drawBlocks(); saveCallouts(); };
$('#coOut').oninput=e=>{ if(selCo<0)return; COUTS[selCo].t1=+e.target.value; drawBlocks(); saveCallouts(); };
$('#coDel').onclick=delSel;
$('#tzoom').oninput=e=>{ tl.style.width=(+e.target.value*100)+'%'; };   // horizontal timeline zoom (scrolls)
// ---- undo / redo: snapshot the timeline model (clips), debounced per gesture ----
let hist=[], hidx=-1, htimer=null;
const snap=()=>JSON.stringify({r:regions,s:speedSegs});
function applySnap(j){ const o=JSON.parse(j); regions=o.r; speedSegs=o.s; clearSel(); recompute(); drawBlocks(); showInsp(); }
function histInit(){ hist=[snap()]; hidx=0; updUR(); }
function commit(){ const c=snap(); saveSession(); if(hidx>=0&&hist[hidx]===c) return; hist=hist.slice(0,hidx+1); hist.push(c); hidx=hist.length-1; if(hist.length>120){hist.shift();hidx--;} updUR(); }
function schedule(){ clearTimeout(htimer); htimer=setTimeout(commit,260); }   // coalesce a drag/slider sweep into one entry
function updUR(){ $('#undo').disabled=hidx<=0; $('#redo').disabled=hidx>=hist.length-1; }
function undo(){ clearTimeout(htimer); commit(); if(hidx>0){ hidx--; applySnap(hist[hidx]); updUR(); } }
function redo(){ clearTimeout(htimer); if(hidx<hist.length-1){ hidx++; applySnap(hist[hidx]); updUR(); } }
$('#undo').onclick=undo; $('#redo').onclick=redo;
$('#addz').onclick=()=>{ const t=v.currentTime||0, nc=nearestClick(t);   // add a zoom at the playhead
  regions.push({t0:Math.max(0,t-0.8),t1:Math.min(dur,t+0.8),z:+$('#s_zoom').value,cx:nc[0],cy:nc[1]});
  regions.sort((a,b)=>a.t0-b.t0); clearSel(); drawBlocks(); recompute(); commit(); };
document.addEventListener('keydown',e=>{
  const mod=e.metaKey||e.ctrlKey, tag=(e.target.tagName||'').toLowerCase(),
        inField=(tag==='input'||tag==='textarea'||tag==='select'||e.target.isContentEditable);
  if(mod && (e.key==='z'||e.key==='Z')){ e.preventDefault(); e.shiftKey?redo():undo(); return; }
  if(mod && (e.key==='y'||e.key==='Y')){ e.preventDefault(); redo(); return; }
  if(inField) return;                                   // don't hijack typing in the inspector
  if(e.key===' '){ e.preventDefault(); $('#playbtn').click(); return; }        // space = play/pause (NLE)
  // Premiere behaviour: a single TAP of ←/→ steps EXACTLY one frame; HOLDING it (OS key-repeat)
  // plays continuously in that direction at normal (1x) speed until release. e.repeat distinguishes
  // the two — the first keydown (repeat=false) is the tap; the auto-repeats start the jog.
  if(e.key==='ArrowLeft'||e.key==='ArrowRight'){ e.preventDefault();
    const sign=(e.key==='ArrowRight')?1:-1;
    if(e.repeat) startScrub(sign);                              // held → continuous 1x jog (normal speed)
    else { stopScrub(); stepFrame(sign*(e.shiftKey?10:1)); }    // tap → exactly one frame (⇧ = 10)
    return; }
  if(e.key==='Home'){ e.preventDefault(); const M=speedMap(); _stepTF=0; v.currentTime=O2S(0,M); return; }
  if(e.key==='End'){ e.preventDefault(); const M=speedMap(); _stepTF=totalOutFrames(M); v.currentTime=O2S(M.outDur,M); return; }
  if((e.key==='Delete'||e.key==='Backspace') && selMulti.length){ e.preventDefault(); delSel(); } });
document.addEventListener('keyup',e=>{ if(e.key==='ArrowLeft'||e.key==='ArrowRight') stopScrub(); });
// step N OUTPUT frames, frame-accurate. Holding an arrow fires keydown repeatedly (OS key
// repeat); a seek can still be in flight when the next repeat arrives, so v.currentTime reads
// stale and a naive "current frame + dir" would stall on the same frame. We keep _stepTF = the
// last COMMANDED output-frame and advance from it; it's resynced to the real playhead whenever
// the user seeks by other means (seek handler clears it) or it drifts far from reality.
// Frame stepping authority: _stepTF is the COMMANDED output frame. stepFrame advances it
// directly and never reads v.currentTime back (a seek may still be in flight on a held key,
// so reading back stalls). It's resynced to the live playhead lazily: whenever it's null we
// take the current frame; clearStepTarget() nulls it on any OTHER seek (timeline/trim/goto/
// play), so the next arrow press picks up the real position.
let _stepTF=null;
function clearStepTarget(){ _stepTF=null; }
// single-frame nudge (one tap of ←/→). Sets the commanded frame and seeks once.
function stepFrame(dir){ if(!v.paused){ v.pause(); $('#playbtn').textContent='▶'; }
  const M=speedMap(), tot=totalOutFrames(M);
  if(_stepTF==null) _stepTF=oFrame(S2O(v.currentTime||0,M));   // resync to real playhead
  _stepTF=clamp(_stepTF+dir,0,tot);
  _seekBusy=true; v.currentTime=O2S(_stepTF/FPS,M); }
// ---- held-arrow smooth scrub -----------------------------------------------
// ROOT CAUSE of the stall on the heavy 4800x2700 all-intra source: each v.currentTime= triggers
// a full ~31MP intra-frame decode; OS key-repeat fires faster than that decode completes, so
// seeks pile up and coalesce — the visible frame freezes while keydowns accumulate. Fix: drive
// the scrub from rAF and gate seeks on the <video> 'seeked' event (_seekBusy). We advance the
// commanded frame on a steady wall-clock cadence but only ISSUE a new seek once the prior one
// landed, so the decoder is never backed up and the playhead moves smoothly at the rate it can
// actually render. _stepTF stays the source of truth for the readout (frame-exact regardless).
// DIAGNOSIS (this source): the lag is the heavy per-seek DECODE, not a code bug. Each
// v.currentTime= forces a full ~31MP (4800x2700) all-intra frame decode that takes ~300ms;
// gating the scrub on each decode therefore crawls at ~3 fps. So we DECOUPLE: the commanded
// frame _stepTF (which drives the playhead + readout) advances on a steady wall-clock cadence
// regardless of decode, giving a smooth-gliding scrub; the actual <video> seek COALESCES to the
// LATEST commanded frame and is only issued when the decoder is free (_seekBusy) — intermediate
// frames the decoder can't keep up with are simply skipped, so the picture catches up to the
// playhead instead of backing the decoder up and stalling. Smooth on any source weight.
let _seekBusy=false, _scrubDir=0, _scrubRAF=null, _scrubLast=0, _scrubAccum=0;
v.addEventListener('seeked',()=>{ _seekBusy=false; });
const SCRUB_MS=1000/FPS;   // held-key jog = exactly normal speed (one frame per 1/FPS sec = 1x playback)
function commitScrubSeek(M){ // seek toward the commanded frame, skipping anything the decoder missed
  if(_seekBusy||_stepTF==null) return;
  const want=O2S(_stepTF/FPS,M);
  if(Math.abs(v.currentTime-want)>1e-3){ _seekBusy=true; v.currentTime=want; } }
function startScrub(dir){ _scrubDir=dir;
  if(_scrubRAF!=null) return;                          // already scrubbing (held key)
  if(!v.paused){ v.pause(); $('#playbtn').textContent='▶'; }
  const M=speedMap(); if(_stepTF==null) _stepTF=oFrame(S2O(v.currentTime||0,M));
  _stepTF=clamp(_stepTF+dir,0,totalOutFrames(M)); commitScrubSeek(M);   // immediate first frame
  _scrubLast=performance.now(); _scrubAccum=0; _scrubRAF=requestAnimationFrame(scrubTick); }
function scrubTick(now){
  const M=speedMap(), tot=totalOutFrames(M);
  const dt=now-_scrubLast; _scrubLast=now; _scrubAccum+=dt;
  // advance the COMMANDED frame on a steady cadence (independent of decode) so playhead+readout glide
  while(_scrubAccum>=SCRUB_MS){ _scrubAccum-=SCRUB_MS;
    if(_stepTF==null) _stepTF=oFrame(S2O(v.currentTime||0,M));
    _stepTF=clamp(_stepTF+_scrubDir,0,tot); }
  commitScrubSeek(M);                                  // issue a coalesced seek toward _stepTF when free
  _scrubRAF=requestAnimationFrame(scrubTick); }
function stopScrub(){ if(_scrubRAF!=null){ cancelAnimationFrame(_scrubRAF); _scrubRAF=null; } _scrubDir=0;
  const M=speedMap(); commitScrubSeek(M); }            // final settle on the exact released frame
// ---- source clip trim (in/out) ---------------------------------------------
// trimIn/trimOut are SOURCE seconds. The remap is NOT clipped to them (speedMap spans the full
// source), so the ruler never reflows and nothing is dropped on save. Trim is shown as greyed
// bands over the timeline at the trimmed ends, and playback loops within [trimIn,trimOut].
function setTrim(ti,to,{commit:doCommit=true}={}){
  ti=clamp(ti||0,0,dur||0);
  to=(to&&to>0)?clamp(to,ti+0.1,dur||to):0;           // 0 = "to source end"
  if(to>0 && to-ti<0.1) to=Math.min(dur,ti+0.1);
  trimIn=ti; trimOut=to; clearStepTarget();
  // pull playback into the new window
  const lo=trimIn, hh=trimHi();
  if(v.currentTime<lo||v.currentTime>hh) v.currentTime=lo;
  drawTrimHandles();
  if(doCommit) saveSession();
}
// Grey out the trimmed-OUT ends on the OUTPUT ruler and draw a draggable handle at each
// inner edge. Bands: left=[0,S2O(trimIn)], right=[S2O(trimOut),outDur]. Handles ALWAYS show
// (at the kept-range boundaries) so the user can grab them even from an un-trimmed clip;
// they ride the OUTPUT positions of trimIn/trimOut, NOT the container edges, so they move
// with the trim and the bands appear only over the actually-excluded ranges.
function drawTrimHandles(){
  [...tl.querySelectorAll('.trimHandle,.trimGrey')].forEach(e=>e.remove());
  if(!dur) return;
  const M=speedMap(); curOD=M.outDur;
  const oIn=S2O(clamp(trimIn,0,dur),M), oOut=S2O(trimHi(),M);
  // LEFT greyed band (only when something is trimmed off the head)
  if(trimIn>0.001){
    const g=document.createElement('div'); g.className='trimGrey';
    g.style.left='0'; g.style.width=Math.max(0,oPct(oIn))+'%'; tl.appendChild(g); }
  // RIGHT greyed band (only when something is trimmed off the tail)
  if(trimOut>0.001 && trimOut<dur-0.001){
    const g=document.createElement('div'); g.className='trimGrey';
    g.style.left=oPct(oOut)+'%'; g.style.width=Math.max(0,oPct(curOD)-oPct(oOut))+'%'; tl.appendChild(g); }
  // handles always present so the clip can be trimmed from scratch
  const hl=document.createElement('div'); hl.className='trimHandle l'; hl.style.left=oPct(oIn)+'%';
  hl.title='Trim in: drag right'; hl.onmousedown=ev=>startTrimDrag(ev,'l'); tl.appendChild(hl);
  const hr=document.createElement('div'); hr.className='trimHandle r'; hr.style.left=oPct(oOut)+'%';
  hr.title='Trim out: drag left'; hr.onmousedown=ev=>startTrimDrag(ev,'r'); tl.appendChild(hr);
}
// Drag a trim handle: map mouse-x across the ruler to OUTPUT time, then back to SOURCE seconds
// (O2S) — the handle sets the in/out point at the dropped source position. Full source unchanged.
function startTrimDrag(e,side){ e.preventDefault(); e.stopPropagation();
  const rect=tl.getBoundingClientRect(), M=speedMap();
  function mv(ev){ const st=O2S(mouseO(ev,rect,M),M);
    if(side==='l') setTrim(st,trimOut,{commit:false}); else setTrim(trimIn,st,{commit:false}); }
  function up(){ document.removeEventListener('mousemove',mv); document.removeEventListener('mouseup',up); saveSession(); }
  document.addEventListener('mousemove',mv); document.addEventListener('mouseup',up); }
// Mouse x -> OUTPUT time on the fixed ruler (1 px is always the same output-seconds).
const mouseO=(ev,rect,M)=>clamp((ev.clientX-rect.left)/rect.width,0,1)*M.outDur;   // cursor frac -> output time (ruler fills width)
// Zoom blocks don't change the speed map, so M captured at drag-start stays valid.
function startDrag(e,i,mode){e.preventDefault();
  if(mode==='move' && (e.metaKey||e.ctrlKey||e.shiftKey)){ clickSelect('zoom',i,e); drawBlocks(); showInsp(); return; }  // modifier = (multi-)select, no drag
  clickSelect('zoom',i,e); drawBlocks();showInsp();
  const r=regions[i],rect=tl.getBoundingClientRect(),M=speedMap(),t0=r.t0,t1=r.t1,w=t1-t0;
  const oo=ev=>O2S(mouseO(ev,rect,M),M);                                // mouse -> source time
  const grab=oo(e)-t0;                                                  // source offset of grab point
  function mv(ev){
    if(mode==='move'){r.t0=clamp(oo(ev)-grab,0,dur-w);r.t1=r.t0+w;}
    else if(mode==='l'){r.t0=clamp(oo(ev),0,r.t1-0.2);}   // trim left edge -> t0 (min 0.2s window, no invert)
    else{r.t1=clamp(oo(ev),r.t0+0.2,dur);}                 // trim right edge -> t1
    drawBlocks();recompute();showInsp();}                  // showInsp -> live duration readout while trimming
  function up(){document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);schedule();}
  document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);}
// NLE-standard idle clip: body = select + move, edges = trim (move/trim the SOURCE range
// of the sped-up region). SPEED is edited only via the inspector (a labeled control), not a
// drag gesture. To avoid feedback (this segment's own speed warps the output-time ruler as we
// drag it), we map the mouse against a speed-map that EXCLUDES the dragged segment — that map
// is constant for the whole drag, so mouse-output -> source-time is exact.
function speedMapExcept(skip){
  const on=$('#f_speedup').checked, segs=on?speedSegs.filter((_,k)=>k!==skip).sort((a,b)=>a.t0-b.t0):[];
  const lo=0, hi=Math.max(0.01,dur||1);   // full source; trim never clips the remap (see speedMap)
  const table=[]; let cur=lo,out=0;
  for(const s of segs){ let t0=Math.max(cur,s.t0),t1=Math.min(hi,s.t1),sp=Math.max(1,s.speed);
    if(t1-t0<0.01)continue;
    if(t0>cur){table.push({s0:cur,s1:t0,sp:1,o0:out});out+=t0-cur;}
    table.push({s0:t0,s1:t1,sp,o0:out});out+=(t1-t0)/sp;cur=t1; }
  if(cur<hi){table.push({s0:cur,s1:hi,sp:1,o0:out});out+=hi-cur;}
  if(!table.length){table.push({s0:lo,s1:hi,sp:1,o0:0});out=hi-lo;}
  return {table,outDur:out};
}
function startSpeedDrag(e,i,mode){e.preventDefault();
  if(mode==='move' && (e.metaKey||e.ctrlKey||e.shiftKey)){ clickSelect('speed',i,e); drawBlocks(); showInsp(); return; }  // modifier = (multi-)select, no drag
  clickSelect('speed',i,e);drawBlocks();showInsp();
  const s=speedSegs[i],rect=tl.getBoundingClientRect(),Mx=speedMapExcept(i),t0=s.t0,t1=s.t1,w=t1-t0;
  const oo=ev=>O2S(mouseO(ev,rect,Mx),Mx); // mouse output -> source (stable map, excludes dragged seg; outDur of Mx, not dur)
  const grab=oo(e)-t0;
  function mv(ev){
    if(mode==='move'){s.t0=clamp(oo(ev)-grab,0,dur-w);s.t1=s.t0+w;}
    else if(mode==='l'){s.t0=clamp(oo(ev),0,s.t1-0.3);}
    else{s.t1=clamp(oo(ev),s.t0+0.3,dur);}
    drawBlocks();showInsp();}
  function up(){document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);schedule();}
  document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);}
function nearestClick(t){if(!A.clicks.length)return[W/2,Hh/2];let b=A.clicks[0];
  for(const c of A.clicks)if(Math.abs(c.t-t)<Math.abs(b.t-t))b=c;return[b.x,b.y];}
// lane bands (px from tl top) -> selectable lane name (keys/clicks lanes aren't selectable)
function laneAtY(y){ if(y>=0&&y<42)return 'zoom'; if(y>=42&&y<72)return 'speed'; if(y>=72&&y<100)return 'co'; return null; }
// output-time [o0,o1] range of block i in `lane` (zoom/speed map source->output; callouts already output)
function blockORange(lane,i,M){ if(lane==='co'){ const c=COUTS[i]; return [+c.t0||0,+c.t1||0]; }
  const r=(lane==='zoom'?regions:speedSegs)[i]; return [S2O(r.t0,M),S2O(r.t1,M)]; }
// rubber-band selection: on EMPTY lane background, drag a rectangle; on move, select every
// block in the dragged lane whose output-time range intersects the dragged x-range. A plain
// click (no movement past threshold) falls back to clear-selection + seek (original behavior).
tl.addEventListener('mousedown',e=>{ if(e.target!==tl && e.target!==ph && !e.target.classList.contains('lane') && !e.target.classList.contains('lanelab') && !e.target.classList.contains('tlend'))return;
  if(e.metaKey||e.ctrlKey||e.shiftKey){ /* don't clear an in-progress modifier multi-select on bg click */ }
  const rect=tl.getBoundingClientRect(), x0=e.clientX-rect.left, y0=e.clientY-rect.top;
  let band=null, dragging=false;
  function mv(ev){ const x=ev.clientX-rect.left, y=ev.clientY-rect.top;
    if(!dragging && Math.abs(x-x0)<4 && Math.abs(y-y0)<4) return;      // movement threshold
    dragging=true;
    const lx=Math.min(x,x0), rx=Math.max(x,x0), ty=Math.min(y,y0), by=Math.max(y,y0);
    if(!band){ band=document.createElement('div'); band.className='rubber'; tl.appendChild(band); }
    band.style.left=lx+'px'; band.style.top=ty+'px'; band.style.width=(rx-lx)+'px'; band.style.height=(by-ty)+'px';
    // pick the selectable lane the drag vertically covers (the start lane is the anchor)
    const lane=laneAtY(y0) || laneAtY(ty) || laneAtY(by); if(!lane){ return; }
    const M=speedMap(), w=rect.width, oL=(lx/w)*M.outDur, oR=(rx/w)*M.outDur;   // x px -> output time
    const arr=(lane==='zoom'?regions:lane==='speed'?speedSegs:COUTS), s=[];
    for(let i=0;i<arr.length;i++){ const [a,b]=blockORange(lane,i,M); if(b>=oL && a<=oR) s.push(i); }
    selLane=lane; selMulti=s; lastClick=s.length?s[s.length-1]:-1; syncSel(); drawBlocks(); showInsp(); }
  function up(ev){ document.removeEventListener('mousemove',mv); document.removeEventListener('mouseup',up);
    if(band){ band.remove(); band=null; }
    if(!dragging){ clearSel(); drawBlocks(); showInsp();                       // plain click = clear + seek
      const M=speedMap(); clearStepTarget(); v.currentTime=O2S(mouseO(e,rect,M),M); } }
  document.addEventListener('mousemove',mv); document.addEventListener('mouseup',up); });
tl.addEventListener('dblclick',e=>{ if(e.target!==tl && e.target!==ph && !e.target.classList.contains('tlend'))return;
  const rect=tl.getBoundingClientRect(),M=speedMap(),o=mouseO(e,rect,M),y=e.clientY-rect.top;
  if(y>=72 && y<100){            // Text track -> add a callout at this output time
    const rd=x=>Math.round(x*100)/100;
    COUTS.push({text:'',t0:rd(o),t1:rd(Math.min(o+2.5,M.outDur)),anchor:'bottom'});
    selectOne('co',COUTS.length-1); drawBlocks(); showInsp(); return; }
  const t=O2S(o,M),[cx,cy]=nearestClick(t);
  regions.push({t0:Math.max(0,t-0.8),t1:Math.min(dur,t+0.8),z:+$('#s_zoom').value,cx,cy});
  regions.sort((a,b)=>a.t0-b.t0);clearSel();drawBlocks();recompute();commit();});
function audioOn(){ if(!actx){ try{actx=new (window.AudioContext||window.webkitAudioContext)();}catch(e){}
    if(actx) fetch('/click').then(r=>r.arrayBuffer()).then(b=>actx.decodeAudioData(b)).then(buf=>clickBuf=buf).catch(()=>{}); }
  if(actx&&actx.state==='suspended')actx.resume(); }
$('#playbtn').onclick=()=>{ audioOn(); clearStepTarget(); if(v.paused){v.play();$('#playbtn').textContent='⏸';}else{v.pause();$('#playbtn').textContent='▶';} };
document.addEventListener('pointerdown',audioOn,{once:true});
$('#s_ramp').oninput=e=>{ $('#l_ramp').textContent=(+e.target.value).toFixed(2)+' s'; recompute(); };
$('#s_zoom').oninput=e=>$('#l_zoom').textContent=(+e.target.value).toFixed(1)+'×';
$('#s_pad').oninput=e=>$('#l_pad').textContent=Math.round(e.target.value*100)+'%';
$('#s_radius').oninput=e=>$('#l_radius').textContent=e.target.value+' px';
document.querySelectorAll('.seg').forEach(s=>s.querySelectorAll('button').forEach(b=>b.onclick=()=>{
  s.querySelectorAll('button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  if(s.id==='seg_ar') setCanvas();
  if(s.id==='seg_ease') recompute();}));   // easing curve change → re-ease preview (rAF tick redraws)
$('#f_speedup').onchange=()=>drawBlocks();
$('#f_keys').onchange=()=>drawBlocks();   // show/hide the keystroke-chip track
async function load(){
  loadPresets();
  A=await(await fetch('/analyze')).json(); dur=A.dur; W=A.w; Hh=A.h; setCanvas();
  regions=A.regions.map(r=>({...r})); speedSegs=(A.idle||[]).map(s=>({t0:s.t0,t1:s.t1,speed:8}));
  try{ const cs=await(await fetch('/callouts')).json(); if(Array.isArray(cs)&&cs.length) COUTS=cs.map(c=>({...c})); }catch(e){}
  try{ const ss=await(await fetch('/session')).json();   // restore manual reframes/pans + speed edits if present
    if(ss&&Array.isArray(ss.regions)&&ss.regions.length) regions=ss.regions.map(r=>({...r}));
    if(ss&&Array.isArray(ss.speedSegs)&&ss.speedSegs.length) speedSegs=ss.speedSegs.map(s=>({...s}));
    if(ss&&ss.curve) setSeg('seg_ease',ss.curve);
    if(ss&&ss.ramp!=null){$('#s_ramp').value=ss.ramp;$('#l_ramp').textContent=(+ss.ramp).toFixed(2)+' s';}
    if(ss&&ss.trimIn!=null) trimIn=+ss.trimIn||0;
    if(ss&&ss.trimOut!=null) trimOut=+ss.trimOut||0; }catch(e){}
  drawTrimHandles();
  histInit();
  if(/[?&]present=1/.test(location.search+location.hash)) document.querySelector('.app').classList.add('present');
  v.src=A.video; v.muted=true; v.loop=true;
  v.addEventListener('loadeddata',()=>{ W=v.videoWidth||W; Hh=v.videoHeight||Hh;
    recompute(); drawBlocks(); v.play().then(()=>$('#playbtn').textContent='⏸').catch(()=>{}); });
  recompute(); drawBlocks(); requestAnimationFrame(tick);
}
const fitMode=()=>document.querySelector('#seg_fit .on').dataset.v;
const dlFile=(url,name)=>{ const u=url+(url.includes('?')?'&':'?')+'dl='+encodeURIComponent(name);   // server sets the save-as name
  const a=document.createElement('a'); a.href=u; a.download=name; document.body.appendChild(a); a.click(); a.remove(); };
$('#reset').onclick=()=>{   // restore the auto-detected zooms + speed-ups
  regions=A.regions.map(r=>({...r})); speedSegs=(A.idle||[]).map(s=>({t0:s.t0,t1:s.t1,speed:8}));
  clearSel(); recompute(); drawBlocks(); showInsp(); commit(); $('#stat').textContent='reset to auto-detected'; };
const pring=$('#pring'), prfg=pring.querySelector('.rfg'), PRC=2*Math.PI*15.5;
prfg.style.strokeDasharray=PRC; const setRing=f=>{ prfg.style.strokeDashoffset=PRC*(1-clamp(f,0,1)); };
// ---- text-callout track: add at playhead (＋ Text) + drag a block to retime ----
// (callouts are OUTPUT time -> mouse maps directly; selection/editing happens in the inspector)
$('#addt').onclick=()=>{ const M=speedMap(), o=S2O(v.currentTime||0,M), OD=M.outDur||6, rd=x=>Math.round(x*100)/100;
  COUTS.push({text:'',t0:rd(o),t1:rd(Math.min(o+2.5,OD)),anchor:'bottom'});   // add at the playhead
  selectOne('co',COUTS.length-1); drawBlocks(); showInsp(); saveCallouts();
  const ct=$('#coText'); if(ct) ct.focus(); };
function startCoDrag(e,i,mode){e.preventDefault();
  if(mode==='move' && (e.metaKey||e.ctrlKey||e.shiftKey)){ clickSelect('co',i,e); drawBlocks(); showInsp(); return; }  // modifier = (multi-)select, no drag
  clickSelect('co',i,e);drawBlocks();showInsp();
  const c=COUTS[i],rect=tl.getBoundingClientRect(),M=speedMap(),OD=M.outDur,w=(+c.t1)-(+c.t0);
  const mo=ev=>clamp((ev.clientX-rect.left)/rect.width,0,1)*OD, rd=x=>Math.round(x*100)/100;
  const grab=mo(e)-(+c.t0);
  function mv(ev){
    if(mode==='move'){c.t0=clamp(mo(ev)-grab,0,Math.max(0,OD-w));c.t1=c.t0+w;}
    else if(mode==='l'){c.t0=clamp(mo(ev),0,c.t1-0.2);}
    else{c.t1=clamp(mo(ev),c.t0+0.2,OD);}
    c.t0=rd(c.t0);c.t1=rd(c.t1); drawBlocks(); showInsp();}   // live-update the inspector's in/out
  function up(){document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up); saveCallouts();}
  document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);}
// ---- direct manipulation of callouts IN THE PREVIEW: drag to move (snaps to the 9 inset
// anchors), drag the corner grip to resize. Hover shows the cursor + a tooltip on the handles. ----
function pvPt(e){ const r=pv.getBoundingClientRect(); return [(e.clientX-r.left)*pv.width/r.width,(e.clientY-r.top)*pv.height/r.height]; }
const GRIP=()=>Math.max(12,pv.width*0.014);
function coHit(px,py){
  if(selCo>=0 && coBounds[selCo]){ const b=coBounds[selCo],g=GRIP();
    if(px>=b.x+b.W-g&&px<=b.x+b.W+5&&py>=b.y+b.H-g&&py<=b.y+b.H+5) return {i:selCo,grip:true}; }
  for(let i=COUTS.length-1;i>=0;i--){ const b=coBounds[i]; if(!b)continue;
    if(px>=b.x&&px<=b.x+b.W&&py>=b.y&&py<=b.y+b.H) return {i,grip:false}; }
  return null; }
function anchorFromXY(px,py){ const col=px<pv.width/3?0:px<2*pv.width/3?1:2, row=py<pv.height/3?0:py<2*pv.height/3?1:2;
  return [['top-left','top','top-right'],['left','center','right'],['bottom-left','bottom','bottom-right']][row][col]; }
// crop-rect hit test on the EDITED keyframe's rect: handle? body? (callouts take priority)
function inRect(px,py,R,pad){ pad=pad||0; return R&&px>=R.x-pad&&px<=R.x+R.w+pad&&py>=R.y-pad&&py<=R.y+R.h+pad; }
function cropHit(px,py){
  if(sel<0||!cropOv) return null;
  const pan=cropOv.end!=null, R=(pan&&kfEdit==='end')?cropOv.end:cropOv.start, g=CHANDLE();
  if(!R) return null;
  if(px>=R.x+R.w-g&&px<=R.x+R.w+6&&py>=R.y+R.h-g&&py<=R.y+R.h+6) return {grip:true};
  if(inRect(px,py,R)) return {grip:false};
  return null;
}
pv.onmousedown=e=>{ const [px,py]=pvPt(e);
  const co=coHit(px,py);
  if(co){ e.preventDefault(); selectOne('co',co.i); showInsp(); drawBlocks();
    const c=COUTS[co.i], topY=(coBounds[co.i]||{}).y||0, resize=co.grip;
    function mv(ev){ const [mx,my]=pvPt(ev);
      if(resize){ c.size=clamp((Math.max(8,my-topY))/1.84/pv.height,0.02,0.09); }
      else { c.anchor=anchorFromXY(mx,my); }
      showInsp(); }
    function up(){ document.removeEventListener('mousemove',mv); document.removeEventListener('mouseup',up); saveCallouts(); }
    document.addEventListener('mousemove',mv); document.addEventListener('mouseup',up); return; }
  // crop-rect reframe drag for the selected zoom clip
  const ch=cropHit(px,py); if(!ch) return; e.preventDefault();
  const r=regions[sel], C=cropOv.content, srcPerPx=W/C.w;            // canvas px -> source px
  const pan=cropOv.end!=null, end=(pan&&kfEdit==='end');
  const cxKey=end?'cx1':'cx', cyKey=end?'cy1':'cy', zKey=end?'z1':'z';
  const cx0=+r[cxKey];                                               // edited keyframe's current values
  const cy0=(r[cyKey]!==undefined&&r[cyKey]!=='')?+r[cyKey]:+r.cy;
  const R0=end?cropOv.end:cropOv.start, sx0=px, sy0=py;             // grab anchors (canvas px)
  function mv(ev){ const [mx,my]=pvPt(ev);
    if(ch.grip){ // corner drag -> change zoom: rect width fraction -> z in [1,2.8]
      const newW=clamp(mx-R0.x, C.w/2.8, C.w);
      let z=Math.round(clamp(C.w/newW,1,2.8)*100)/100; r[zKey]=z;
    } else { // body drag -> move center of the edited keyframe
      r[cxKey]=clamp(cx0+(mx-sx0)*srcPerPx,0,W);
      r[cyKey]=clamp(cy0+(my-sy0)*srcPerPx,0,Hh);
    }
    recompute(); showInsp(); schedule(); }
  function up(){ document.removeEventListener('mousemove',mv); document.removeEventListener('mouseup',up); commit(); }
  document.addEventListener('mousemove',mv); document.addEventListener('mouseup',up); };
pv.onmousemove=e=>{ const [px,py]=pvPt(e),h=coHit(px,py);
  if(h){ pv.style.cursor=h.grip?'nwse-resize':'move'; pv.title=h.grip?'Drag to resize':'Drag to move — snaps to the edges'; return; }
  const ch=cropHit(px,py);
  pv.style.cursor=ch?(ch.grip?'nwse-resize':'grab'):'default';
  pv.title=ch?(ch.grip?'Drag to change zoom level':'Drag to reframe (move where the zoom looks)'):''; };
async function runExport(gif){   // render the mp4 (real progress ring); gif=true also transcodes to GIF
  const feats=['speedup','clickfx','keys'].filter(f=>$('#f_'+f).checked), fr=frame();
  const speedSegments=speedSegs.map(s=>({t0:s.t0,t1:s.t1,speed:s.speed}));
  $('#go').disabled=$('#gif').disabled=true; (gif?$('#gif'):$('#go')).textContent=gif?'GIF…':'Rendering…'; $('#errbox').innerHTML='';
  pring.style.display=''; setRing(0.02); $('#stat').textContent='rendering '+aspect()+(gif?' → gif':'');
  const poll=setInterval(async()=>{ try{ const p=await(await fetch('/progress')).json();
    if(p.n>0){ setRing(p.i/p.n); $('#stat').textContent='rendering '+Math.round(100*p.i/p.n)+'%'; } }catch(e){} },300);
  try{ const r=await(await fetch('/render',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({regions,feats:feats.join(','),aspect:aspect(),fit:fitMode(),ramp:$('#s_ramp').value,curve:easeMode(),speedSegments,
        callouts:COUTS,bg:fr.bg,pad:fr.pad,radius:fr.radius,shadow:fr.shadow,gif:!!gif,trimIn,trimOut})})).json();
    clearInterval(poll);
    if(r.error){ $('#errbox').innerHTML='<div class=err>'+r.error+'</div>'; $('#stat').textContent='export failed'; }
    else if(gif){ if(r.gif){ setRing(1); dlFile(r.gif, r.gifName||'studio-export.gif'); $('#stat').textContent='✓ '+(r.gifName||'gif'); }
      else{ $('#errbox').innerHTML='<div class=err>GIF failed: '+(r.gifError||'unknown')+'</div>'; $('#stat').textContent='gif failed'; } }
    else{ setRing(1); const name=r.name||'studio-export.mp4'; dlFile('/download?f=export.mp4',name); $('#stat').textContent='✓ '+name; }
  }catch(e){ clearInterval(poll); $('#errbox').innerHTML='<div class=err>'+e+'</div>'; $('#stat').textContent='export failed'; }
  setTimeout(()=>{pring.style.display='none';},500);
  $('#go').disabled=$('#gif').disabled=false; $('#go').textContent='Export video'; $('#gif').textContent='GIF'; }
$('#go').onclick=()=>runExport(false);
$('#gif').onclick=()=>runExport(true);
// ---- style presets (save/reapply a look) ----
let PRESETS={};
function gatherStyle(){ return {aspect:aspect(), fit:fitMode(), bg:bgsel(),
  pad:+$('#s_pad').value, radius:+$('#s_radius').value, shadow:$('#f_shadow').checked,
  ramp:+$('#s_ramp').value, zoom:+$('#s_zoom').value, curve:easeMode(),
  speedup:$('#f_speedup').checked, clickfx:$('#f_clickfx').checked, keys:$('#f_keys').checked }; }
function setSeg(id,val){ if(val==null)return; document.querySelectorAll('#'+id+' button').forEach(b=>b.classList.toggle('on',b.dataset.v===val)); }
function applyStyle(s){ if(!s)return;
  setSeg('seg_ar',s.aspect); setSeg('seg_fit',s.fit); setSeg('seg_bg',s.bg);
  if(s.pad!=null){$('#s_pad').value=s.pad;$('#l_pad').textContent=Math.round(s.pad*100)+'%';}
  if(s.radius!=null){$('#s_radius').value=s.radius;$('#l_radius').textContent=s.radius+' px';}
  if(s.shadow!=null)$('#f_shadow').checked=s.shadow;
  if(s.ramp!=null){$('#s_ramp').value=s.ramp;$('#l_ramp').textContent=(+s.ramp).toFixed(2)+' s';}
  if(s.curve!=null)setSeg('seg_ease',s.curve);
  if(s.zoom!=null){$('#s_zoom').value=s.zoom;$('#l_zoom').textContent=(+s.zoom).toFixed(1)+'×';}
  if(s.speedup!=null)$('#f_speedup').checked=s.speedup;
  if(s.clickfx!=null)$('#f_clickfx').checked=s.clickfx;
  if(s.keys!=null)$('#f_keys').checked=s.keys;
  setCanvas(); recompute(); }
async function loadPresets(){ try{ PRESETS=await(await fetch('/presets')).json(); }catch(e){ PRESETS={}; }
  const sel=$('#presetSel'), keys=Object.keys(PRESETS).sort();
  sel.innerHTML='<option value="">— preset —</option>'+keys.map(k=>'<option>'+k.replace(/</g,'&lt;')+'</option>').join(''); }
$('#presetSel').onchange=e=>{ applyStyle(PRESETS[e.target.value]); };
$('#presetSave').onclick=async()=>{ const name=($('#presetName').value||'').trim(); if(!name)return;
  PRESETS=await(await fetch('/presets',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({save:name,settings:gatherStyle()})})).json();
  $('#presetName').value=''; await loadPresets(); $('#presetSel').value=name; $('#stat').textContent='saved preset “'+name+'”'; };
$('#presetDel').onclick=async()=>{ const name=$('#presetSel').value; if(!name)return;
  await fetch('/presets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({delete:name})});
  await loadPresets(); $('#stat').textContent='deleted preset'; };
$('#fcx').onclick=async()=>{   // non-destructive handoff: write + download FCPXML
  const feats=['speedup','clickfx'].filter(f=>$('#f_'+f).checked);
  const speedSegments=speedSegs.map(s=>({t0:s.t0,t1:s.t1,speed:s.speed}));
  const b=$('#fcx'); b.disabled=true; b.textContent='Writing .fcpxml…'; $('#errbox').innerHTML='';
  try{ const r=await(await fetch('/fcpxml',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({regions,feats:feats.join(','),ramp:$('#s_ramp').value,curve:easeMode(),speedSegments})})).json();
    if(r.error){ $('#errbox').innerHTML='<div class=err>'+r.error+'</div>'; }
    else{ dlFile(r.file, r.name||'studio-export.fcpxml'); $('#fcstat').textContent='✓ downloaded — import with “use sizing information”'; }
  }catch(e){ $('#errbox').innerHTML='<div class=err>'+e+'</div>'; }
  b.disabled=false; b.textContent='Export for editing (FCPXML) →'; };
load();
</script></body></html>"""

if __name__ == "__main__":
    port = free_port()
    print(f"studio: http://127.0.0.1:{port}   (source: {os.path.basename(SRC_VIDEO)})", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1",port), H).serve_forever()
