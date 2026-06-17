#!/usr/bin/env python3
"""iteration.py — the per-iteration deliverable for this project: a single
"video + UI" pipeline clip. Puts the Studio UI (editable timeline + controls) and
the rendered output side by side, with a flow divider, so every iteration shows BOTH
the controls and what they produced.

  iteration.py --ui ui.png --video rendered.mp4 [--vertical v.mp4] --out iteration.mp4

The UI panel is static (a screenshot); the rendered output plays. Use after a render:
capture the studio page, then run this against the rendered file(s).
"""
import argparse, os, subprocess, sys
from PIL import Image, ImageDraw, ImageFont

def vdur(path):
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","csv=p=0",path], capture_output=True, text=True)
    return float(r.stdout.strip())

H = 760  # panel height
FONT = "/System/Library/Fonts/Helvetica.ttc"

def fit_h(img, h):
    w = int(round(img.width * h / img.height / 2) * 2)
    return img.resize((w, h), Image.LANCZOS)

def divider(h):
    """Dark strip with an arrow + stage labels: EDIT → RENDER."""
    w = 150
    img = Image.new("RGB", (w, h), (14, 15, 19))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 20); fs = ImageFont.truetype(FONT, 13)
    cy = h // 2
    d.line([(20, cy), (w - 28, cy)], fill=(95, 208, 192), width=3)
    d.polygon([(w - 28, cy - 9), (w - 28, cy + 9), (w - 10, cy)], fill=(95, 208, 192))
    d.text((26, cy - 40), "edit", font=fs, fill=(138, 144, 162))
    d.text((20, cy - 22), "timeline", font=fs, fill=(231, 233, 239))
    d.text((40, cy + 14), "spring", font=fs, fill=(138, 144, 162))
    d.text((34, cy + 30), "render", font=fs, fill=(231, 233, 239))
    return img

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui", required=True); ap.add_argument("--video", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--label", default="")
    a = ap.parse_args()
    tmp = "/tmp"
    ui = fit_h(Image.open(a.ui).convert("RGB"), H)
    ui.save(f"{tmp}/_it_ui.png")
    divider(H).save(f"{tmp}/_it_div.png")
    # scale the rendered video to height H, hstack: [UI][divider][video]
    fc = (f"[2:v]scale=-2:{H}:flags=lanczos[v];"
          f"[0:v][1:v][v]hstack=inputs=3[o]")
    # -t bounds the looped image inputs to the video length (-shortest is unreliable
    # with image2 loop inputs — it can run forever and orphan the ffmpeg child).
    dur = vdur(a.video)
    subprocess.run(["ffmpeg","-y","-loglevel","error",
        "-loop","1","-i",f"{tmp}/_it_ui.png",
        "-loop","1","-i",f"{tmp}/_it_div.png",
        "-i",a.video,
        "-filter_complex",fc,"-map","[o]",
        "-t",f"{dur:.3f}",
        "-c:v","libx264","-crf","20","-pix_fmt","yuv420p","-r","30",
        "-movflags","+faststart",a.out], check=True)
    os.remove(f"{tmp}/_it_ui.png"); os.remove(f"{tmp}/_it_div.png")
    sz = os.path.getsize(a.out)
    print(f"wrote {a.out} ({sz//1024} KB)")

if __name__ == "__main__":
    main()
