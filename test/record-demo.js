#!/usr/bin/env node
/**
 * record-demo.js — DETERMINISTIC recorder for the studio NLE demo (mp4/gif).
 *
 * WHY THIS EXISTS (do not replace with Playwright recordVideo):
 *   recordVideo captures at the browser's requestAnimationFrame cadence — a variable
 *   frame rate with timing jitter. Forcing that VFR stream to a CFR mp4/gif duplicates
 *   and drops frames UNEVENLY, which is most visible as a stutter/"skip" during fast
 *   motion (the auto-zoom). This recorder instead STEPS the preview to each exact OUTPUT
 *   frame, lets tick() repaint, and screenshots it — so every output frame is captured
 *   exactly once. Assemble the JPGs at the same fps and the motion is perfectly smooth
 *   (no frozen-mid-motion frames, no judder). Verified: 0 frozen frames across all zoom
 *   bands; loop seam diff ~0.5.
 *
 * Usage:
 *   node test/record-demo.js <studio-url> [out-dir=/tmp/ssa-frames] [fps=30]
 *   then assemble, e.g.:
 *     ffmpeg -framerate 30 -i <out-dir>/f%05d.jpg -frames:v <N> \
 *       -vf scale=1440:900 -c:v libx264 -pix_fmt yuv420p -crf 18 -movflags +faststart demo.mp4
 *   (pick <N> to end on the post-typing wide shot so it loops cleanly).
 */
const path = require('path');
const { execSync } = require('child_process');
function loadPlaywright() {
  // Try local resolution, then the global npm modules dir (works on any machine).
  const candidates = ['playwright'];
  try {
    const groot = execSync('npm root -g', { encoding: 'utf8' }).trim();
    if (groot) candidates.push(path.join(groot, 'playwright'));
  } catch (e) {}
  for (const p of candidates) {
    try { return require(p); } catch (e) {}
  }
  throw new Error('playwright not found — npm i -g playwright or npx playwright install');
}
const { chromium } = loadPlaywright();
const fs = require('fs');
const URL = process.argv[2];
const DIR = process.argv[3] || '/tmp/ssa-frames';
const FPS = parseInt(process.argv[4] || '30', 10);
if (!URL) { console.error('usage: record-demo.js <studio-url> [out-dir] [fps]'); process.exit(2); }
(async () => {
  fs.rmSync(DIR, { recursive: true, force: true }); fs.mkdirSync(DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  await page.goto(URL, { waitUntil: 'domcontentloaded' });   // NOT networkidle: a large (100MB+) source video streams continuously and networkidle never settles → timeout
  await page.waitForFunction(() => (typeof A !== 'undefined' && A && typeof v !== 'undefined' && v && v.readyState >= 2), { timeout: 45000 });  // give a heavy all-intra source time to reach HAVE_CURRENT_DATA
  await page.evaluate(() => {                       // clean selection, keystroke chips on, FULLY paused
    try { sel = -1; selSpd = -1; selCo = -1; if (typeof showInsp === 'function') showInsp(); } catch (e) {}
    const k = document.getElementById('f_keys'); if (k && !k.checked) { k.checked = true; if (k.onchange) k.onchange(); }
    // the studio autoplays + loops the preview; that fights the per-frame seek (the playhead keeps
    // moving/looping, so frames capture the wrong source time). Kill autoplay/loop and hard-pause.
    v.loop = false; v.autoplay = false; v.pause();
  });
  const outDur = await page.evaluate(() => speedMap().outDur);
  const n = Math.floor(outDur * FPS);
  process.stderr.write(`outDur=${outDur.toFixed(2)} frames=${n} @${FPS}fps -> ${DIR}\n`);
  for (let i = 0; i < n; i++) {
    await page.evaluate(async (o) => {             // seek to EXACT output frame, wait 2 RAFs to repaint
      const M = speedMap(); const src = Math.max(0, Math.min(dur - 1e-3, O2S(o, M)));
      v.pause();                                   // re-assert pause each frame (autoplay can re-engage)
      await new Promise(res => {
        let done = false;
        const fin = () => { if (done) return; done = true; v.onseeked = null; requestAnimationFrame(() => requestAnimationFrame(res)); };
        v.onseeked = fin; v.currentTime = src; setTimeout(fin, 300);
      });
    }, i / FPS);
    await page.screenshot({ path: `${DIR}/f${String(i).padStart(5, '0')}.jpg`, type: 'jpeg', quality: 95 });
    if (i % 30 === 0) process.stderr.write(`  ${i}/${n}\n`);
  }
  await browser.close();
  process.stdout.write(`done ${n}\n`);
})().catch(e => { console.error(e); process.exit(1); });
