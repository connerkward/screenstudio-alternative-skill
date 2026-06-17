#!/usr/bin/env python3
"""make-fixture.py — synthesize a fake screen recording + matching events.jsonl.

Deterministic ground truth for testing polish.py features:
  0.0– 3.0  cursor travels from center to top-left button, CLICKS x3 (t=1.2, 1.9, 2.6)
  3.0– 9.0  IDLE (frozen frame)                                  <- speedup must compress
  9.0–12.0  cursor travels to bottom-right panel, CLICKS x2 (t=10.2, 11.0)
 12.0–16.0  typing into the panel: "hello demo!" appears char-by-char (keys logged)
 16.0–22.0  IDLE (frozen frame)                                  <- speedup must compress
 22.0–24.0  cursor drifts back toward center (motion, no clicks)

Usage: make-fixture.py out.mp4 events.jsonl [--no-cursor]
"""
import json, math, subprocess, sys, tempfile, os
from PIL import Image, ImageDraw, ImageFont

W, H, FPS, DUR = 1280, 800, 30, 24.0
NO_CURSOR = "--no-cursor" in sys.argv
OUT_MP4, OUT_EVENTS = sys.argv[1], sys.argv[2]

def _load_font(size):
    """Portable sans-serif (macOS/Linux/Windows) → Pillow default; so the fixture
    builds on any CI runner, not just macOS."""
    for p in ("/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try: return ImageFont.truetype(p, size)
        except OSError: continue
    try: return ImageFont.load_default(size)
    except TypeError: return ImageFont.load_default()
f_ui   = _load_font(22)
f_body = _load_font(26)

BTN  = (180, 160)        # top-left button center (click target 1)
PANEL= (980, 600)        # bottom-right panel center (click target 2)
CENTER = (W//2, H//2)

TYPED = "hello demo!"
TYPE_T0, TYPE_CPS = 12.0, 3.0          # chars appear at 3 cps
# clicks fire AFTER the cursor has arrived at the target (so a ripple never precedes the mouse)
CLICKS = [(1.5,*BTN),(2.1,*BTN),(2.7,*BTN),(10.3,*PANEL),(11.1,*PANEL)]

def ease(a): return 0.5 - 0.5*math.cos(math.pi*max(0.0,min(1.0,a)))

def lerp(p,q,a): return (p[0]+(q[0]-p[0])*a, p[1]+(q[1]-p[1])*a)

def cursor_pos(t):
    if t < 1.1:    return lerp(CENTER, BTN, ease(t/1.1))               # ARRIVE button ~1.1s
    if t < 9.0:    return BTN                                          # idle (still)
    if t < 9.9:    return lerp(BTN, PANEL, ease((t-9.0)/0.9))         # ARRIVE panel ~9.9s
    if t < 22.0:   return PANEL                                        # typing + idle (still)
    return lerp(PANEL, CENTER, ease((t-22.0)/2.0))

def typed_at(t):
    if t < TYPE_T0: return ""
    n = int((t-TYPE_T0)*TYPE_CPS)
    return TYPED[:max(0,min(len(TYPED),n))]

def draw_frame(t):
    img = Image.new("RGB",(W,H),(238,238,242))
    d = ImageDraw.Draw(img)
    # fake app chrome
    d.rectangle([0,0,W,52],fill=(52,54,66))
    d.text((20,14),"Fixture App — synthetic screen recording",font=f_ui,fill=(235,235,240))
    for gx in range(0,W,80): d.line([(gx,52),(gx,H)],fill=(225,225,230))
    for gy in range(52,H,80): d.line([(0,gy),(W,gy)],fill=(225,225,230))
    # button (target 1) — depresses briefly on each click
    pressed = any(abs(t-ct)<0.15 for ct,cx,cy in CLICKS[:3])
    d.rounded_rectangle([BTN[0]-70,BTN[1]-26,BTN[0]+70,BTN[1]+26],10,
                        fill=(70,120,220) if not pressed else (40,80,170))
    d.text((BTN[0]-18,BTN[1]-13),"Run",font=f_ui,fill="white")
    d.polygon([(BTN[0]-42,BTN[1]-8),(BTN[0]-42,BTN[1]+8),(BTN[0]-30,BTN[1])],fill="white")  # play tri
    # panel (target 2) with typed text
    d.rounded_rectangle([PANEL[0]-200,PANEL[1]-60,PANEL[0]+200,PANEL[1]+60],12,
                        fill="white",outline=(180,180,190),width=2)
    d.text((PANEL[0]-185,PANEL[1]-16),typed_at(t)+("|" if 12.0<=t<16.2 else ""),font=f_body,fill=(30,30,40))
    # cursor
    # No baked cursor — the pipeline draws a crisp synthetic cursor from the event log
    # (matches the real `sck-record --no-cursor` capture workflow).
    return img

def main():
    tmp = tempfile.mkdtemp(prefix="fixture-")
    n = int(DUR*FPS)
    for i in range(n):
        draw_frame(i/FPS).save(f"{tmp}/f{i:05d}.png")
        if i % 120 == 0: print(f"  frames {i}/{n}", file=sys.stderr)
    subprocess.run(["ffmpeg","-y","-loglevel","error","-framerate",str(FPS),
        "-i",f"{tmp}/f%05d.png","-c:v","libx264","-pix_fmt","yuv420p","-crf","18",OUT_MP4],check=True)
    for p in os.listdir(tmp): os.remove(os.path.join(tmp,p))
    os.rmdir(tmp)

    # ---- events.jsonl: same coordinate space as the video (scale 1) ----
    ev=[{"type":"header","epoch":0.0,"display":{"w":W,"h":H},"scale":1.0}]
    t=0.0
    while t<DUR:                                   # 30Hz move samples
        x,y=cursor_pos(t); ev.append({"t":round(t,3),"type":"move","x":round(x,1),"y":round(y,1)}); t+=1/30
    for ct,cx,cy in CLICKS:
        ev.append({"t":ct,"type":"down","x":cx,"y":cy,"button":"left"})
        ev.append({"t":round(ct+0.08,3),"type":"up","x":cx,"y":cy,"button":"left"})
    for i,ch in enumerate(TYPED):
        ev.append({"t":round(TYPE_T0+i/TYPE_CPS,3),"type":"key","key":("␣" if ch==" " else ch)})
    ev.sort(key=lambda e:e.get("t",0))
    with open(OUT_EVENTS,"w") as f:
        for e in ev: f.write(json.dumps(e)+"\n")
    print(f"wrote {OUT_MP4} ({DUR}s) + {OUT_EVENTS} ({len(ev)} events)")

if __name__=="__main__": main()
