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
PRESET_LOCK = threading.Lock()       # serialize read-modify-write on the presets store
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
        regions = polish.zoom_regions(ev["clicks"], vid, 2.0, vid["dur"], keys=ev["keys"], moves=ev["moves"])
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
           "--ramp", str(p.get("ramp",0.5)), "--progress-file", prog]
    if SRC_EVENTS: cmd += ["--events", SRC_EVENTS, "--cursor"]   # smooth cursor always on
    if "clickfx" in feats: cmd += ["--clickfx"]
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
            with RENDER_LOCK: return self._send(200, json.dumps(export_fcpxml(payload)))  # bug A
        if u.path == "/presets":
            with PRESET_LOCK: return self._send(200, json.dumps(save_preset(payload)))
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
h1{font-size:16px;font-weight:650;letter-spacing:-.02em;margin:0}h1 .muted{font-weight:400}
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
.time{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums;min-width:92px}
.addz{background:#10121a;border:1px solid var(--line);color:var(--acc);border-radius:8px;height:30px;padding:0 13px;cursor:pointer;font-size:12px;font-weight:600}.addz:hover{border-color:var(--acc)}
.hint{color:var(--dim);font-size:11px;margin-left:auto;text-align:right}
.edrow{display:flex;gap:9px;align-items:flex-start}
.tlscroll{flex:1;min-width:0;overflow-x:auto}
.tzoom{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:13px}
.tzoom input{width:88px;accent-color:var(--acc)}
.gutter{flex:none;width:46px;display:flex;flex-direction:column;font-size:9px;letter-spacing:.05em;text-transform:uppercase;color:#565c6e;padding-top:1px}
.gutter span{display:flex;align-items:center}
.gutter span:nth-child(1){height:48px}.gutter span:nth-child(2){height:36px}.gutter span:nth-child(3){height:28px}
.tl{position:relative;width:100%;min-width:440px;height:112px;background:#0c0e15;border:1px solid var(--line);border-radius:8px;cursor:crosshair;overflow:hidden}
.lane{position:absolute;left:0;right:0;pointer-events:none}
.laneZ{top:0;height:48px;background:#12141d}
.laneS{top:48px;height:36px;background:#0d0f16;border-top:1px solid #1c202b;border-bottom:1px solid #1c202b}
.laneC{top:84px;height:28px;background:#10121a}
.zoomblock{position:absolute;top:7px;height:34px;background:linear-gradient(var(--zoom),#d98b3e);border-radius:7px;cursor:grab;box-shadow:0 2px 6px #0007;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#241500;overflow:hidden;z-index:3}
.zoomblock.sel{outline:2px solid #fff;outline-offset:1px;box-shadow:0 0 0 4px #5fd0c033,0 2px 8px #0008}
.zoomblock .h,.idleb .h{position:absolute;top:0;width:11px;height:100%;cursor:ew-resize;pointer-events:auto;z-index:4}
.zoomblock .h::after,.idleb .h::after{content:"";position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:2px;height:44%;border-radius:2px;background:#ffffff96}
.zoomblock .h.l,.idleb .h.l{left:0}.zoomblock .h.r,.idleb .h.r{right:0}
.zoomblock:hover,.idleb:hover{filter:brightness(1.09)}                       /* signal interactivity */
.zoomblock:hover .h::after,.idleb:hover .h::after{background:#fff;height:60%;width:3px}  /* trim grips pop on hover */
.click{position:absolute;top:90px;width:2px;height:16px;margin-left:-1px;border-radius:1px;background:#6a7488;pointer-events:none;z-index:2}
.idleb{position:absolute;top:52px;height:28px;background:repeating-linear-gradient(45deg,#2a2e3c,#2a2e3c 7px,#222633 7px,#222633 14px);border:1px solid #3a4357;border-radius:5px;cursor:grab;overflow:visible;display:flex;align-items:center;justify-content:center;z-index:2}
.tlend{position:absolute;top:0;height:100%;background:rgba(6,7,11,0.55);border-left:1px solid #2a2e3c;pointer-events:none;z-index:1}
.idleb span{font-size:10px;color:#cfd3df;font-weight:700;letter-spacing:.03em;white-space:nowrap;pointer-events:none}
.ph{position:absolute;top:0;width:2px;height:100%;background:var(--acc);pointer-events:none;z-index:5;box-shadow:0 0 6px #5fd0c0aa}
.idleb.sel{outline:2px solid var(--acc);outline-offset:1px;box-shadow:0 0 0 4px #5fd0c033}
/* Inspector = property panel DOCKED to the right of the timeline (always present) */
.insp{flex:none;width:212px;align-self:stretch;background:#13151c;border:1px solid var(--line);border-radius:9px;padding:10px 12px;display:flex;flex-direction:column}
.insp .ibody{display:none;flex-direction:column;gap:7px;height:100%}
.insp.has-sel .ibody{display:flex}
.insp.has-sel .inspempty{display:none}
.inspempty{margin:auto;text-align:center;color:var(--dim);font-size:12px;line-height:1.6}
.insp .ihead{display:flex;align-items:center;gap:8px}
.insp .ihead .dot{width:9px;height:9px;border-radius:3px;flex:none}
.insp.kind-speed .dot{background:#7c8398}.insp.kind-zoom .dot{background:var(--zoom)}
.insp .ititle{font-size:12px;font-weight:700;color:var(--ink)}
.insp .isub{font-size:10px;color:var(--dim)}
.insp .proprow{display:flex;align-items:center;justify-content:space-between;margin-top:1px}
.insp .plabel{font-size:12px;color:var(--dim)}
.insp .num{display:flex;align-items:center;font-variant-numeric:tabular-nums}
.insp .num input[type=number]{width:50px;background:#10121a;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:3px 6px;font-size:13px;font-weight:700;text-align:right;-moz-appearance:textfield}
.insp .num input[type=number]::-webkit-outer-spin-button,.insp .num input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.insp .num .unit{font-size:11px;color:var(--dim);font-weight:700;margin-left:3px}
.insp input[type=range]{width:100%;accent-color:var(--acc)}
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
  <h1>studio <span class=muted>· screen-recording editor</span></h1>
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
    <details class=adv><summary><span class=chev>▶</span> Frame styling</summary>
      <div class=sl style=margin-top:6px><label>Padding <b id=l_pad>6%</b></label><input type=range id=s_pad min=0 max=0.14 step=0.01 value=0.06></div>
      <div class=sl><label>Corner radius <b id=l_radius>18 px</b></label><input type=range id=s_radius min=0 max=44 step=2 value=18></div>
      <label class=f style=margin-top:6px><input type=checkbox id=f_shadow checked> Drop shadow</label></details>
    <details class=adv><summary><span class=chev>▶</span> Advanced</summary>
      <div class=sl style=margin-top:6px><label>Zoom transition <b id=l_ramp>0.50 s</b></label><input type=range id=s_ramp min=0.2 max=0.9 step=0.05 value=0.5></div>
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
    <label class=tzoom title="Zoom the timeline">⌕<input type=range id=tzoom min=1 max=8 step=0.5 value=1></label>
    <span class=hint id=hint></span>
  </div>
  <div class=edrow>
    <div class=gutter><span>Zoom</span><span>Speed</span><span>Clicks</span></div>
    <div class=tlscroll>
      <div class=tl id=tl>
        <div class="lane laneZ"></div><div class="lane laneS"></div><div class="lane laneC"></div>
        <div class=ph id=ph style=left:0></div>
      </div>
    </div>
    <div class=insp id=insp>
      <div class=inspempty id=inspEmpty>Select a clip<br><span class=mini>to edit speed or zoom</span></div>
      <div class=ibody>
        <div class=ihead><span class=dot></span><span class=ititle id=inspTitle></span></div>
        <div class=isub id=inspSub></div>
        <div class=proprow><span class=plabel id=inspPropLabel>Speed</span><span class=num><input type=number id=inspNum><span class=unit id=inspUnit>×</span></span></div>
        <input type=range id=inspSlider>
        <button class=del id=inspDel>Delete clip</button>
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
let A=null, regions=[], speedSegs=[], sel=-1, selSpd=-1, dur=0, W=0, Hh=0, FPS=60, N=0, zA=[],xA=[],yA=[];
let clickBuf=null;
let actx=null, lastT=-1;
function playTick(){ if(!actx||!clickBuf)return;
  const s=actx.createBufferSource(), g=actx.createGain(); s.buffer=clickBuf; g.gain.value=0.85;
  s.connect(g); g.connect(actx.destination); s.start(actx.currentTime); }
function easeTraj(key, rest){
  const arr=new Array(N).fill(rest), ramp=+$('#s_ramp').value;
  for(const r of [...regions].sort((a,b)=>a.t0-b.t0)){
    const o0=r.t0,o1=r.t1,v=key==='z'?+r.z:(key==='cx'?+r.cx:+r.cy),rmp=Math.min(ramp,(o1-o0)/2);
    for(let i=0;i<N;i++){const t=i/FPS; if(t<o0||t>o1)continue; let f;
      if(rmp>0&&t<o0+rmp){const a=(t-o0)/rmp;f=0.5-0.5*Math.cos(Math.PI*a);}
      else if(rmp>0&&t>o1-rmp){const a=(o1-t)/rmp;f=0.5-0.5*Math.cos(Math.PI*a);}
      else f=1;
      arr[i]=rest+(v-rest)*f;}
  }
  return arr;
}
function recompute(){ if(!dur) return; N=Math.round(dur*FPS)+1;
  zA=easeTraj('z',1); xA=easeTraj('cx',W/2); yA=easeTraj('cy',Hh/2); }
function fmt(t){t=t||0;return Math.floor(t/60)+':'+String(Math.floor(t%60)).padStart(2,'0');}
const AR={'16:9':[1280,720],'1:1':[1080,1080],'9:16':[1080,1920]};
const BG={dark:['#14161e','#0c0d12'],light:['#eef0f4','#d6dbe4'],dusk:['#311c48','#182856'],
  ocean:['#0d223a','#0b4e62'],warm:['#402c1c','#68382a'],mint:['#12342e','#1a4e42'],slate:['#282c38','#161921']};
const aspect=()=>document.querySelector('#seg_ar .on').dataset.v;
const bgsel=()=>document.querySelector('#seg_bg .on').dataset.v;
function frame(){ return {bg:bgsel(), pad:+$('#s_pad').value, radius:+$('#s_radius').value, shadow:$('#f_shadow').checked}; }
function rr(ctx,x,y,w,h,r){ ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath(); }
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
  const table=[]; let cur=0,out=0;
  for(const s of segs){ let t0=Math.max(cur,s.t0),t1=s.t1,sp=Math.max(1,s.speed);
    if(t1-t0<0.01)continue;
    if(t0>cur){table.push({s0:cur,s1:t0,sp:1,o0:out});out+=t0-cur;}
    table.push({s0:t0,s1:t1,sp,o0:out});out+=(t1-t0)/sp;cur=t1; }
  if(cur<dur){table.push({s0:cur,s1:dur,sp:1,o0:out});out+=dur-cur;}
  if(!table.length){table.push({s0:0,s1:dur||1,sp:1,o0:0});out=dur||1;}
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
    if(!v.paused && $('#f_clickfx').checked && A.clicks){
      if(t<lastT) lastT=t;
      for(const c of A.clicks) if(c.t>lastT && c.t<=t) playTick();
      lastT=t;
    } else lastT=t;
    ph.style.left=oPct(S2O(t,M))+'%';               // OUTPUT-time playhead on the fixed ruler
    $('#time').textContent=fmt(S2O(t,M))+' / '+fmt(M.outDur);
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
function drawBlocks(){
  [...tl.querySelectorAll('.zoomblock,.click,.idleb,.tlend')].forEach(e=>e.remove());
  const M=speedMap(); curOD=M.outDur;              // ruler = output duration -> content fills the width
  if($('#f_speedup').checked) speedSegs.forEach((s,si)=>{const e=document.createElement('div');e.className='idleb'+(si===selSpd?' sel':'');
    e.style.left=oPct(S2O(s.t0,M))+'%';e.style.width=(oPct(S2O(s.t1,M))-oPct(S2O(s.t0,M)))+'%';e.style.pointerEvents='auto';
    e.innerHTML='<div class="h l" title="trim"></div><span>⏩ '+(+s.speed).toFixed(0)+'×</span><div class="h r" title="trim"></div>';
    e.onmousedown=ev=>startSpeedDrag(ev,si,'move');                                        // body = select + move
    e.querySelector('.h.l').onmousedown=ev=>{ev.stopPropagation();startSpeedDrag(ev,si,'l');};  // edge = trim
    e.querySelector('.h.r').onmousedown=ev=>{ev.stopPropagation();startSpeedDrag(ev,si,'r');};  // edge = trim
    tl.appendChild(e);});
  A.clicks.forEach(c=>{const d=document.createElement('div');d.className='click';d.title='click';d.style.left=oPct(S2O(c.t,M))+'%';tl.appendChild(d);});
  regions.forEach((r,i)=>{const b=document.createElement('div');b.className='zoomblock'+(i===sel?' sel':'');
    b.style.left=oPct(S2O(r.t0,M))+'%';b.style.width=(oPct(S2O(r.t1,M))-oPct(S2O(r.t0,M)))+'%';
    b.innerHTML='<div class="h l"></div>'+(+r.z).toFixed(1)+'×<div class="h r"></div>';
    b.onmousedown=e=>startDrag(e,i,'move');
    b.querySelector('.h.l').onmousedown=e=>{e.stopPropagation();startDrag(e,i,'l');};
    b.querySelector('.h.r').onmousedown=e=>{e.stopPropagation();startDrag(e,i,'r');};
    b.onwheel=e=>{e.preventDefault();sel=i;selSpd=-1;const rr=regions[i];   // scroll = zoom level
      rr.z=Math.round(clamp(rr.z+(e.deltaY<0?0.1:-0.1),1.2,2.8)*10)/10;drawBlocks();recompute();showInsp();schedule();};
    b.ondblclick=e=>{e.stopPropagation();regions.splice(i,1);sel=-1;drawBlocks();recompute();showInsp();commit();};
    tl.appendChild(b);});
  $('#hint').textContent=regions.length+' zoom · '+speedSegs.length+' speed · '+A.clicks.length+' clicks';
}
function showInsp(){ const insp=$('#insp'), s=$('#inspSlider'), num=$('#inspNum');
  if(sel>=0){ insp.classList.add('has-sel'); insp.classList.add('kind-zoom'); insp.classList.remove('kind-speed');
    const r=regions[sel], len=(r.t1-r.t0);
    $('#inspTitle').textContent='Zoom clip';
    $('#inspSub').textContent=fmt(r.t0)+'–'+fmt(r.t1)+'  ·  '+len.toFixed(1)+'s';
    $('#inspPropLabel').textContent='Zoom';
    s.min=1.2;s.max=2.8;s.step=0.1;s.value=r.z;
    num.min=1.2;num.max=2.8;num.step=0.1;num.value=(+r.z).toFixed(1);
    $('#inspUnit').textContent='×'; }
  else if(selSpd>=0){ insp.classList.add('has-sel'); insp.classList.add('kind-speed'); insp.classList.remove('kind-zoom');
    const sp=speedSegs[selSpd], len=(sp.t1-sp.t0);
    $('#inspTitle').textContent='Idle clip (sped up)';
    $('#inspSub').textContent=len.toFixed(1)+'s source → '+(len/Math.max(1,sp.speed)).toFixed(1)+'s output';
    $('#inspPropLabel').textContent='Speed';
    s.min=2;s.max=16;s.step=1;s.value=sp.speed;
    num.min=2;num.max=16;num.step=1;num.value=(+sp.speed).toFixed(0);
    $('#inspUnit').textContent='×'; }
  else { insp.classList.remove('has-sel'); } }
// keep slider + number in sync, write back to the selected clip
function inspSet(v){
  if(sel>=0){ regions[sel].z=v; $('#inspSlider').value=v; $('#inspNum').value=v.toFixed(1);
    $('#inspSub').textContent=fmt(regions[sel].t0)+'–'+fmt(regions[sel].t1)+'  ·  '+(regions[sel].t1-regions[sel].t0).toFixed(1)+'s';
    drawBlocks(); recompute(); }
  else if(selSpd>=0){ const sp=speedSegs[selSpd]; sp.speed=v; $('#inspSlider').value=v; $('#inspNum').value=v.toFixed(0);
    $('#inspSub').textContent=(sp.t1-sp.t0).toFixed(1)+'s source → '+((sp.t1-sp.t0)/Math.max(1,v)).toFixed(1)+'s output';
    drawBlocks(); } schedule(); }
$('#inspSlider').oninput=e=>inspSet(+e.target.value);
$('#inspNum').oninput=e=>{ let v=+e.target.value; if(isNaN(v))return;
  const lo=+e.target.min,hi=+e.target.max; v=clamp(v,lo,hi); inspSet(v); };
$('#inspNum').onchange=e=>{ let v=clamp(+e.target.value||+e.target.min,+e.target.min,+e.target.max);
  e.target.value = (selSpd>=0)?v.toFixed(0):v.toFixed(1); inspSet(v); };
$('#inspDel').onclick=()=>{ if(sel>=0){ regions.splice(sel,1); sel=-1; drawBlocks(); recompute(); showInsp(); commit(); }
  else if(selSpd>=0){ speedSegs.splice(selSpd,1); selSpd=-1; drawBlocks(); showInsp(); commit(); } };
$('#tzoom').oninput=e=>{ tl.style.width=(+e.target.value*100)+'%'; };   // horizontal timeline zoom (scrolls)
// ---- undo / redo: snapshot the timeline model (clips), debounced per gesture ----
let hist=[], hidx=-1, htimer=null;
const snap=()=>JSON.stringify({r:regions,s:speedSegs});
function applySnap(j){ const o=JSON.parse(j); regions=o.r; speedSegs=o.s; sel=-1; selSpd=-1; recompute(); drawBlocks(); showInsp(); }
function histInit(){ hist=[snap()]; hidx=0; updUR(); }
function commit(){ const c=snap(); if(hidx>=0&&hist[hidx]===c) return; hist=hist.slice(0,hidx+1); hist.push(c); hidx=hist.length-1; if(hist.length>120){hist.shift();hidx--;} updUR(); }
function schedule(){ clearTimeout(htimer); htimer=setTimeout(commit,260); }   // coalesce a drag/slider sweep into one entry
function updUR(){ $('#undo').disabled=hidx<=0; $('#redo').disabled=hidx>=hist.length-1; }
function undo(){ clearTimeout(htimer); commit(); if(hidx>0){ hidx--; applySnap(hist[hidx]); updUR(); } }
function redo(){ clearTimeout(htimer); if(hidx<hist.length-1){ hidx++; applySnap(hist[hidx]); updUR(); } }
$('#undo').onclick=undo; $('#redo').onclick=redo;
$('#addz').onclick=()=>{ const t=v.currentTime||0, nc=nearestClick(t);   // add a zoom at the playhead
  regions.push({t0:Math.max(0,t-0.8),t1:Math.min(dur,t+0.8),z:+$('#s_zoom').value,cx:nc[0],cy:nc[1]});
  regions.sort((a,b)=>a.t0-b.t0); sel=-1; selSpd=-1; drawBlocks(); recompute(); commit(); };
document.addEventListener('keydown',e=>{
  const mod=e.metaKey||e.ctrlKey, tag=(e.target.tagName||'').toLowerCase(),
        inField=(tag==='input'||tag==='textarea'||tag==='select'||e.target.isContentEditable);
  if(mod && (e.key==='z'||e.key==='Z')){ e.preventDefault(); e.shiftKey?redo():undo(); return; }
  if(mod && (e.key==='y'||e.key==='Y')){ e.preventDefault(); redo(); return; }
  if((e.key==='Delete'||e.key==='Backspace') && !inField && (sel>=0||selSpd>=0)){ e.preventDefault(); $('#inspDel').click(); } });
// Mouse x -> OUTPUT time on the fixed ruler (1 px is always the same output-seconds).
const mouseO=(ev,rect,M)=>clamp((ev.clientX-rect.left)/rect.width,0,1)*M.outDur;   // cursor frac -> output time (ruler fills width)
// Zoom blocks don't change the speed map, so M captured at drag-start stays valid.
function startDrag(e,i,mode){e.preventDefault();sel=i;selSpd=-1;drawBlocks();showInsp();
  const r=regions[i],rect=tl.getBoundingClientRect(),M=speedMap(),t0=r.t0,t1=r.t1,w=t1-t0;
  const oo=ev=>O2S(mouseO(ev,rect,M),M);                                // mouse -> source time
  const grab=oo(e)-t0;                                                  // source offset of grab point
  function mv(ev){
    if(mode==='move'){r.t0=clamp(oo(ev)-grab,0,dur-w);r.t1=r.t0+w;}
    else if(mode==='l'){r.t0=clamp(oo(ev),0,r.t1-0.3);}
    else{r.t1=clamp(oo(ev),r.t0+0.3,dur);}
    drawBlocks();recompute();}
  function up(){document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);schedule();}
  document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up);}
// NLE-standard idle clip: body = select + move, edges = trim (move/trim the SOURCE range
// of the sped-up region). SPEED is edited only via the inspector (a labeled control), not a
// drag gesture. To avoid feedback (this segment's own speed warps the output-time ruler as we
// drag it), we map the mouse against a speed-map that EXCLUDES the dragged segment — that map
// is constant for the whole drag, so mouse-output -> source-time is exact.
function speedMapExcept(skip){
  const on=$('#f_speedup').checked, segs=on?speedSegs.filter((_,k)=>k!==skip).sort((a,b)=>a.t0-b.t0):[];
  const table=[]; let cur=0,out=0;
  for(const s of segs){ let t0=Math.max(cur,s.t0),t1=s.t1,sp=Math.max(1,s.speed);
    if(t1-t0<0.01)continue;
    if(t0>cur){table.push({s0:cur,s1:t0,sp:1,o0:out});out+=t0-cur;}
    table.push({s0:t0,s1:t1,sp,o0:out});out+=(t1-t0)/sp;cur=t1; }
  if(cur<dur){table.push({s0:cur,s1:dur,sp:1,o0:out});out+=dur-cur;}
  if(!table.length){table.push({s0:0,s1:dur||1,sp:1,o0:0});out=dur||1;}
  return {table,outDur:out};
}
function startSpeedDrag(e,i,mode){e.preventDefault();selSpd=i;sel=-1;drawBlocks();showInsp();
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
tl.addEventListener('mousedown',e=>{ if(e.target!==tl && e.target!==ph && !e.target.classList.contains('lane') && !e.target.classList.contains('lanelab') && !e.target.classList.contains('tlend'))return;
  sel=-1;selSpd=-1;drawBlocks();showInsp();
  const rect=tl.getBoundingClientRect(),M=speedMap(); v.currentTime=O2S(mouseO(e,rect,M),M);});
tl.addEventListener('dblclick',e=>{ if(e.target!==tl && e.target!==ph && !e.target.classList.contains('tlend'))return;
  const rect=tl.getBoundingClientRect(),M=speedMap(),t=O2S(mouseO(e,rect,M),M),[cx,cy]=nearestClick(t);
  regions.push({t0:Math.max(0,t-0.8),t1:Math.min(dur,t+0.8),z:+$('#s_zoom').value,cx,cy});
  regions.sort((a,b)=>a.t0-b.t0);sel=-1;drawBlocks();recompute();commit();});
function audioOn(){ if(!actx){ try{actx=new (window.AudioContext||window.webkitAudioContext)();}catch(e){}
    if(actx) fetch('/click').then(r=>r.arrayBuffer()).then(b=>actx.decodeAudioData(b)).then(buf=>clickBuf=buf).catch(()=>{}); }
  if(actx&&actx.state==='suspended')actx.resume(); }
$('#playbtn').onclick=()=>{ audioOn(); if(v.paused){v.play();$('#playbtn').textContent='⏸';}else{v.pause();$('#playbtn').textContent='▶';} };
document.addEventListener('pointerdown',audioOn,{once:true});
$('#s_ramp').oninput=e=>{ $('#l_ramp').textContent=(+e.target.value).toFixed(2)+' s'; recompute(); };
$('#s_zoom').oninput=e=>$('#l_zoom').textContent=(+e.target.value).toFixed(1)+'×';
$('#s_pad').oninput=e=>$('#l_pad').textContent=Math.round(e.target.value*100)+'%';
$('#s_radius').oninput=e=>$('#l_radius').textContent=e.target.value+' px';
document.querySelectorAll('.seg').forEach(s=>s.querySelectorAll('button').forEach(b=>b.onclick=()=>{
  s.querySelectorAll('button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  if(s.id==='seg_ar') setCanvas();}));
$('#f_speedup').onchange=()=>drawBlocks();
async function load(){
  loadPresets();
  A=await(await fetch('/analyze')).json(); dur=A.dur; W=A.w; Hh=A.h; setCanvas();
  regions=A.regions.map(r=>({...r})); speedSegs=(A.idle||[]).map(s=>({t0:s.t0,t1:s.t1,speed:8}));
  histInit();
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
  sel=-1; selSpd=-1; recompute(); drawBlocks(); showInsp(); commit(); $('#stat').textContent='reset to auto-detected'; };
const pring=$('#pring'), prfg=pring.querySelector('.rfg'), PRC=2*Math.PI*15.5;
prfg.style.strokeDasharray=PRC; const setRing=f=>{ prfg.style.strokeDashoffset=PRC*(1-clamp(f,0,1)); };
async function runExport(gif){   // render the mp4 (real progress ring); gif=true also transcodes to GIF
  const feats=['speedup','clickfx'].filter(f=>$('#f_'+f).checked), fr=frame();
  const speedSegments=speedSegs.map(s=>({t0:s.t0,t1:s.t1,speed:s.speed}));
  $('#go').disabled=$('#gif').disabled=true; (gif?$('#gif'):$('#go')).textContent=gif?'GIF…':'Rendering…'; $('#errbox').innerHTML='';
  pring.style.display=''; setRing(0.02); $('#stat').textContent='rendering '+aspect()+(gif?' → gif':'');
  const poll=setInterval(async()=>{ try{ const p=await(await fetch('/progress')).json();
    if(p.n>0){ setRing(p.i/p.n); $('#stat').textContent='rendering '+Math.round(100*p.i/p.n)+'%'; } }catch(e){} },300);
  try{ const r=await(await fetch('/render',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({regions,feats:feats.join(','),aspect:aspect(),fit:fitMode(),ramp:$('#s_ramp').value,speedSegments,
        bg:fr.bg,pad:fr.pad,radius:fr.radius,shadow:fr.shadow,gif:!!gif})})).json();
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
  ramp:+$('#s_ramp').value, zoom:+$('#s_zoom').value,
  speedup:$('#f_speedup').checked, clickfx:$('#f_clickfx').checked }; }
function setSeg(id,val){ if(val==null)return; document.querySelectorAll('#'+id+' button').forEach(b=>b.classList.toggle('on',b.dataset.v===val)); }
function applyStyle(s){ if(!s)return;
  setSeg('seg_ar',s.aspect); setSeg('seg_fit',s.fit); setSeg('seg_bg',s.bg);
  if(s.pad!=null){$('#s_pad').value=s.pad;$('#l_pad').textContent=Math.round(s.pad*100)+'%';}
  if(s.radius!=null){$('#s_radius').value=s.radius;$('#l_radius').textContent=s.radius+' px';}
  if(s.shadow!=null)$('#f_shadow').checked=s.shadow;
  if(s.ramp!=null){$('#s_ramp').value=s.ramp;$('#l_ramp').textContent=(+s.ramp).toFixed(2)+' s';}
  if(s.zoom!=null){$('#s_zoom').value=s.zoom;$('#l_zoom').textContent=(+s.zoom).toFixed(1)+'×';}
  if(s.speedup!=null)$('#f_speedup').checked=s.speedup;
  if(s.clickfx!=null)$('#f_clickfx').checked=s.clickfx;
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
      body:JSON.stringify({regions,feats:feats.join(','),ramp:$('#s_ramp').value,speedSegments})})).json();
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
