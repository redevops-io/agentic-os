// Social Autopilot — product-demo reel recorder (vertical / 9:16, for Shorts & Reels).
// Drives a real Chrome through a live walkthrough and screen-records it to MP4:
//   Intro  · redevops.io home → v1 diagram → v2 diagram (title cards, ~4.5s each)
//   1) chat.redevops.io      — type a live query, watch the answer
//   2) redevops.io/planner   — type a query, "Run live" (EXPLAIN decision)
//   3) redevops.io/benchmarks — scroll the v1→v2 results
//   Outro  · redevops.io "What the runtime does" panel (~5s)
// puppeteer-core (system Chrome) + puppeteer-screen-recorder (ffmpeg). ~50-55s, 1080x1920.
//
//   OUT=reel.mp4 CHAT=... PLANNER=... BENCH=... node record_reel.mjs
import puppeteer from 'puppeteer-core';
import { PuppeteerScreenRecorder } from 'puppeteer-screen-recorder';

const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome';
const FFMPEG = process.env.FFMPEG_PATH || '/usr/bin/ffmpeg';
const OUT = process.env.OUT || 'reel.mp4';
const HOME = process.env.HOME_URL || 'https://redevops.io/';
const HERO = process.env.HERO_URL || 'file://' + process.cwd() + '/assets/hero.jpg';
const V1 = process.env.V1_URL || 'https://redevops.io/media/whitepaper/context-runtime-v1.jpg';
const V2 = process.env.V2_URL || 'https://redevops.io/media/whitepaper/context-runtime-v2.jpg';
const CHAT = process.env.CHAT_URL || 'https://chat.redevops.io/';
const PLANNER = process.env.PLANNER_URL || 'https://redevops.io/planner/';
const BENCH = process.env.BENCH_URL || 'https://redevops.io/benchmarks/';
const CHAT_Q = process.env.CHAT_Q || 'What drove the change in gross margin last year?';
const PLAN_Q = process.env.PLAN_Q || 'summarize the incident root cause';
const SLIDE = Number(process.env.SLIDE_MS || 4500);   // per intro title card
const W = 1080, H = 1920;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log('[reel]', ...a);

async function typeInto(page, sels, text) {
  for (const s of sels) {
    const el = await page.$(s);
    if (el) {
      await el.click({ delay: 40 }).catch(() => {});
      await page.keyboard.type(text, { delay: 85 });
      return el;
    }
  }
  return null;
}

// Dismiss the cookie/consent banner so it never covers the lower cards.
async function dismissCookie(page) {
  try {
    await page.evaluate(() => {
      const btn = [...document.querySelectorAll('button,a')].find(
        (b) => /^(accept|reject|opt out|reject \/ opt out)/i.test((b.textContent || '').trim()));
      if (btn) btn.click();
    });
    await sleep(500);
  } catch { /* no banner */ }
}

const browser = await puppeteer.launch({
  executablePath: CHROME, headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--hide-scrollbars',
         `--window-size=${W},${H}`],
  defaultViewport: { width: W, height: H },
});
const page = await browser.newPage();
const recorder = new PuppeteerScreenRecorder(page, {
  fps: 25, ffmpeg_Path: FFMPEG, videoFrame: { width: W, height: H },
  aspectRatio: '9:16', followNewTab: false,
});
// Pre-load the hero title card (a static JPEG of the redevops.io hero) BEFORE recording
// starts, so the very first frame is already the clean hero — no load flash.
try {
  await page.goto(HERO, { waitUntil: 'load', timeout: 20000 });
  await sleep(400);
} catch (e) { log('preload error', String(e).slice(0, 120)); }

await recorder.start(OUT);

// ── Intro · title cards (hero → v1 → v2) ───────────────────────────────
try {
  log('intro: hero');
  await sleep(4000);                              // the provided hero JPEG for ~4s
  for (const [name, url] of [['v1', V1], ['v2', V2]]) {
    log('intro:', name);
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
    await sleep(SLIDE);
  }
} catch (e) { log('intro error', String(e).slice(0, 120)); }

// ── Scene 1 · live chat ────────────────────────────────────────────────
try {
  log('scene 1: chat');
  await page.goto(CHAT, { waitUntil: 'networkidle2', timeout: 45000 });
  await sleep(2800);
  const input = await typeInto(page, [
    'form textarea', 'textarea[placeholder*="Message" i]', '#prompt-textarea',
    'textarea[name="text"]', 'div[contenteditable="true"]', 'textarea',
  ], CHAT_Q);
  await sleep(700);
  if (input) {
    await page.keyboard.press('Enter');          // LibreChat sends on Enter
    await sleep(9000);                            // let the answer stream in
  } else {
    log('chat input not found — showing the page'); await sleep(4000);
  }
} catch (e) { log('scene 1 error', String(e).slice(0, 120)); await sleep(1500); }

// ── Scene 2 · live planner (EXPLAIN) ───────────────────────────────────
try {
  log('scene 2: planner');
  await page.goto(PLANNER, { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2500);
  const q = await typeInto(page, ['#q', 'input[placeholder*="live runtime" i]', 'input'], PLAN_Q);
  await sleep(600);
  if (q) {
    const run = await page.$('#run') || await page.$('button.run');
    if (run) await run.click();
    await sleep(7000);                            // EXPLAIN result renders
  } else { await sleep(4000); }
} catch (e) { log('scene 2 error', String(e).slice(0, 120)); await sleep(1500); }

// ── Scene 3 · benchmarks (scroll the table) ────────────────────────────
try {
  log('scene 3: benchmarks');
  await page.goto(BENCH, { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2200);
  const steps = 12;
  for (let i = 0; i < steps; i++) {
    await page.evaluate(() => window.scrollBy({ top: 260, behavior: 'smooth' }));
    await sleep(470);
  }
  await sleep(1000);
} catch (e) { log('scene 3 error', String(e).slice(0, 120)); }

// ── Outro · "What the runtime does" ────────────────────────────────────
try {
  log('outro: what the runtime does');
  await page.goto(HOME, { waitUntil: 'networkidle2', timeout: 45000 });
  await dismissCookie(page);
  await page.evaluate(() => {
    const heading = (re) => [...document.querySelectorAll('h1,h2,h3')].find((n) => re.test(n.textContent || ''));
    const does = heading(/what the runtime does/i);
    if (!does) return;
    // Isolate the "What the runtime does" panel — hide its sibling sections (the reference-app
    // cards above, and the "Also from ReDevOps" products + footer below) plus the sticky nav,
    // so the outro frames only this panel on the dark background.
    const sec = does.closest('section') || does.parentElement;
    const parent = sec.parentElement;
    if (parent) [...parent.children].forEach((c) => { if (c !== sec) c.style.display = 'none'; });
    document.querySelectorAll('header,nav').forEach((e) => {
      const p = getComputedStyle(e).position;
      if (p === 'fixed' || p === 'sticky') e.style.display = 'none';
    });
    sec.scrollIntoView({ block: 'center' });
  });
  await sleep(5000);
} catch (e) { log('outro error', String(e).slice(0, 120)); }

await recorder.stop();
await browser.close();
log('done ->', OUT);
