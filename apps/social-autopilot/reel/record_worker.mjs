// Host-side UI-agent worker for the product-demo reel.
// Watches ./spool/record.request and records the walkthrough to ./out/reel.mp4 using the HOST's
// Chrome (headless Chrome can't run inside a container on this host). Writes ./out/status.json so
// the social-autopilot container can report progress and serve the finished MP4.
//
//   CHROME_PATH=/usr/bin/google-chrome node record_worker.mjs
import fs from 'node:fs';
import { execFile } from 'node:child_process';

const DIR = new URL('.', import.meta.url).pathname;        // .../reel/
const OUT_DIR = DIR + 'out';
const OUT = OUT_DIR + '/reel.mp4';
const STATUS = OUT_DIR + '/status.json';
const REQ = DIR + 'spool/record.request';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome';
const FFMPEG = process.env.FFMPEG_PATH || '/usr/bin/ffmpeg';

fs.mkdirSync(OUT_DIR, { recursive: true });
fs.mkdirSync(DIR + 'spool', { recursive: true });

const setStatus = (o) =>
  fs.writeFileSync(STATUS, JSON.stringify({ ts: Math.floor(Date.now() / 1000), duration: null, error: null, ...o }));

let busy = false;
function record() {
  busy = true;
  setStatus({ status: 'recording' });
  console.log('[reel-worker] recording…');
  const env = { ...process.env, OUT, CHROME_PATH: CHROME, FFMPEG_PATH: FFMPEG };
  execFile('node', ['record_reel.mjs'], { cwd: DIR, env, timeout: 300000, maxBuffer: 1e7 }, (err) => {
    if (err) {
      setStatus({ status: 'error', error: String(err).slice(0, 200) });
      console.log('[reel-worker] error', String(err).slice(0, 160));
      busy = false;
      return;
    }
    execFile(FFMPEG.replace(/ffmpeg$/, 'ffprobe'), ['-v', 'error', '-show_entries', 'format=duration',
      '-of', 'default=nw=1:nk=1', OUT], (e, so) => {
      const dur = e ? null : Math.round(parseFloat(so) || 0);
      setStatus({ status: 'done', duration: dur });
      console.log('[reel-worker] done', dur, 's');
      busy = false;
    });
  });
}

if (!fs.existsSync(STATUS)) setStatus({ status: 'idle' });
setInterval(() => {
  if (!busy && fs.existsSync(REQ)) {
    try { fs.rmSync(REQ); } catch {}
    record();
  }
}, 3000);
console.log('[reel-worker] watching', REQ, '· chrome', CHROME);
