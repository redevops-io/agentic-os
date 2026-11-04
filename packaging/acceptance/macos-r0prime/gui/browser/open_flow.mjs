// Controlled browser for the redirect-page flow: headless Chromium, disposable profile.
// Usage (env CHROMIUM, UD, OUT): node open_flow.mjs <redirect-file>
// Writes OUT/flow-<ts>.json. Never prints a launch code or token.
const pw = (await import(process.env.PWCORE)).default;
import { pathToFileURL } from 'url';
import fs from 'fs';
const file = process.argv[2];
const t0 = Date.now();
const rec = { file, t_start: t0, events: [] };
const ctx = await pw.chromium.launchPersistentContext(process.env.UD + '-' + t0, { executablePath: process.env.CHROMIUM, headless: true });
const page = ctx.pages()[0] || await ctx.newPage();
page.on('console', m => { if (m.type() === 'error') rec.events.push('console.error: ' + m.text().slice(0, 200)); });
page.on('response', r => { const u = r.url(); if (u.includes('/api/')) rec.events.push(`${r.request().method()} ${new URL(u).pathname} ${r.status()}`); });
let err = null;
try {
  await page.goto(pathToFileURL(file).href, { waitUntil: 'commit' });
  await page.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\//, { timeout: 20000 });
  rec.t_landed_ms = Date.now() - t0;
  await page.waitForTimeout(2500);
} catch (e) { err = String(e).slice(0, 300); }
rec.error = err;
rec.final_url_has_fragment = page.url().includes('#');
rec.final_url = page.url().replace(/#.*/, '#<stripped-or-none>');
rec.fragment_present_in_final_url = /#lc=/.test(page.url());
try {
  rec.api_status = await page.evaluate(async () => (await fetch('/api/projects')).status);
  rec.body_has_customer_ops = (await page.content()).includes('Customer Operations');
  rec.session_keys = await page.evaluate(() => Object.keys(sessionStorage));
  rec.title = await page.title();
} catch (e) { rec.eval_error = String(e).slice(0, 200); }
fs.writeFileSync(`${process.env.OUT}/flow-${t0}.json`, JSON.stringify(rec, null, 1));
console.log(JSON.stringify({ file: file.split('/').pop(), landed: rec.t_landed_ms, api: rec.api_status, err }));
await ctx.close();
