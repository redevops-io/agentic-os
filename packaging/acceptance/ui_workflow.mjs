// The installed desktop app's UI, driven in a real browser on the operating system it was installed on: the workflows a person does first
// (see what needs a decision, open one, capture a note, find something, move between sections), keyboard reachability, accessibility
// (axe, WCAG 2 A/AA) and the session boundary. Complements native_check.py, which checks the same build at the process and API level.
//
//   node ui_workflow.mjs <path to the installed redevops-sidecar> <result.json>
//   needs: @playwright/test and axe-core resolvable from $PW_ROOT (a directory with node_modules), Chromium installed for Playwright.
//
// A build either ships a sample workspace (launcher up to 0.1.3) or serves no sample data at all (0.1.4 and later, production profile). The
// script finds out which from the page and checks what that kind of build has: a check that needs sample items is recorded as UNSUPPORTED
// on a build without them, never as passed. Nothing here approves a consequential action.
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import os from 'node:os'; import fs from 'node:fs'; import path from 'node:path';
const require = createRequire(path.join(process.env.PW_ROOT || process.cwd(), 'x.js'));
const { chromium } = require('@playwright/test');
const axeSrc = fs.readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
const [side, outFile] = process.argv.slice(2);
const results = []; const known = []; const t00 = Date.now();
const check = async (name, fn) => { const t = Date.now(); try { const d = await fn(); results.push({ name, status: 'PASS', detail: String(d ?? '').slice(0, 300), ms: Date.now() - t }); console.log('PASS  ' + name + (d ? '  | ' + String(d).slice(0, 160) : '')); }
  catch (e) { results.push({ name, status: 'FAIL', detail: String(e && e.message || e).slice(0, 400), ms: Date.now() - t }); console.log('FAIL  ' + name + '  | ' + String(e && e.message || e).slice(0, 300)); } };
const assert = (c, m) => { if (!c) throw new Error(m); };
const unsupported = (name, why) => { results.push({ name, status: 'UNSUPPORTED', detail: why, ms: 0 }); console.log('UNSUPPORTED  ' + name + '  | ' + why); };
let sample = null;                                    // true: sample workspace; false: no sample data; null: the workspace never rendered

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'aa-ui-'));
const port = 28000 + Math.floor(Math.random() * 900);
const env = { PATH: process.env.PATH, HOME: home, USERPROFILE: home, LOCALAPPDATA: path.join(home, 'Local'), APPDATA: path.join(home, 'AppData'),
  XDG_STATE_HOME: path.join(home, 'state'), TMPDIR: process.env.TMPDIR || os.tmpdir(), TEMP: process.env.TEMP || os.tmpdir(), TMP: process.env.TMP || os.tmpdir(),
  SYSTEMROOT: process.env.SYSTEMROOT || '', LANG: 'C.UTF-8', PROJECTS_PORT: String(port) };
const proc = spawn(side, ['serve', '--exit-on-stdin-eof'], { env, stdio: ['pipe', 'pipe', 'pipe'] });
let log = ''; proc.stdout.on('data', (d) => log += d); proc.stderr.on('data', (d) => log += d);
let url; for (let i = 0; i < 120 && !url && proc.exitCode === null; i++) { await new Promise((r) => setTimeout(r, 1000)); const m = log.match(/https?:\/\/127\.0\.0\.1:\d+\/\S*#lc=[A-Za-z0-9_-]+/); if (m) url = m[0]; }
const origin = url ? new URL(url).origin : '';
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 } });
const p = await ctx.newPage();
const consoleErrors = [], failed = [];
p.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 160)); });
p.on('pageerror', (e) => consoleErrors.push('pageerror: ' + String(e).slice(0, 160)));
p.on('response', (r) => { if (r.status() >= 400 && r.url().startsWith(origin)) failed.push(r.status() + ' ' + r.request().method() + ' ' + new URL(r.url()).pathname); });
const body = async () => (await p.locator('body').innerText()).replace(/\s+/g, ' ');
// The app's Content-Security-Policy refuses inline scripts (as it should), so axe is evaluated through the debugger, not injected as a tag.
const axe = async () => { await p.evaluate(axeSrc); return p.evaluate(async () => (await window.axe.run(document, { runOnly: ['wcag2a', 'wcag2aa'] })).violations.map((v) => v.id + ' x' + v.nodes.length)); };
const posture = (log.match(/redevops-sidecar: profile=[^\r\n]+/) || [''])[0];

await check('the sidecar starts and prints a one-time launch link on loopback', async () => { assert(url, 'no launch link within 120 s: ' + log.slice(-300)); return origin; });
// Launcher 0.1.2 is the first build that selects its profile explicitly. An older release cannot pass this; with KNOWN_NO_PROFILE=1 (set by
// the workflow for exactly those releases) the gap is recorded as KNOWN_DEFECT, which is neither a pass nor a new failure.
if (!posture && process.env.KNOWN_NO_PROFILE === '1') {
  results.push({ name: 'the packaged build states an explicit production profile with durable stores', status: 'KNOWN_DEFECT',
                 detail: 'this release starts with no explicit profile (fixed in launcher 0.1.2)', ms: 0 });
  console.log('KNOWN_DEFECT  this release starts with no explicit profile (fixed in launcher 0.1.2)');
} else await check('the packaged build states an explicit production profile with durable stores', async () => {
  assert(/profile=production; authority and replay stores are durable/.test(posture), 'no posture line, or not production: "' + posture + '" (builds before launcher 0.1.2 start with no explicit profile)'); return posture; });
if (url) {
  await check('the launch link opens the workspace, and the one-time code does not stay in the address bar', async () => {
    await p.goto(url, { waitUntil: 'networkidle' });
    // Wait for Today to settle, not for a fixed time: on a slow machine the view still read "Loading…" after 800 ms, the check failed, and
    // the kind of build (sample data or none) was never determined, so the sample-only checks ran against a build without sample data.
    let t = ''; for (let i = 0; i < 60; i++) { t = await body(); if (/need your decision|nothing needs you/i.test(t)) break; await p.waitForTimeout(500); }
    assert(/need your decision|nothing needs you/i.test(t), 'the Today view did not render within 30 s: ' + t.slice(0, 200));
    sample = /sample data/i.test(t);
    assert(!p.url().includes('#lc='), 'the launch code is still in the URL'); return p.url().replace(origin, ''); });
  await check('the keyboard reaches the content: "Skip to content" is the first stop and moves focus into the main region', async () => {
    await p.keyboard.press('Tab');
    const first = await p.evaluate(() => (document.activeElement && (document.activeElement.innerText || document.activeElement.getAttribute('aria-label')) || '').trim());
    assert(/skip to (main )?content/i.test(first), 'the first tab stop is "' + first + '"'); await p.keyboard.press('Enter'); return first; });
  if (sample !== false) {
    await check('Today lists what needs a decision and says the data is a sample', async () => {
      const t = await body(); const n = Number((t.match(/(\d+) things? need your decision/i) || [])[1] || 0);
      assert(n >= 1, 'no count of items'); assert(/sample data/i.test(t), 'no sample-data notice'); return n + ' items; sample-data notice present'; });
  } else await check('Today says that nothing needs a decision, and shows no sample data', async () => {
    const t = await body(); assert(/nothing needs you/i.test(t), 'no empty-state line: ' + t.slice(0, 200)); assert(!/sample data/i.test(t), 'a sample-data notice on a build that serves none');
    return 'empty state; no sample notice'; });
  await check('Today passes WCAG 2 A/AA (axe)', async () => { const v = await axe(); assert(v.length === 0, v.join(', ')); return 'no violations'; });
  if (sample) {
    await check('an item opens with its detail and its next step', async () => {
      const before = await body();
      const item = p.getByRole('button', { name: /milestone at risk|Visa documents|Quote hasn't|patients haven't/i }).first();
      assert(await item.count(), 'no item button found'); const label = ((await item.innerText()) || '').replace(/\s+/g, ' ').slice(0, 60);
      await item.click(); await p.waitForTimeout(1200);
      let after = await body();
      if (after === before) { const open = p.getByRole('button', { name: /^Open$/ }).first(); if (await open.count()) { await open.click(); await p.waitForTimeout(1200); after = await body(); } }
      const added = after.length - before.length;
      assert(after !== before && /suggest|next step|evidence|trail|approve|why/i.test(after), 'opening the item showed no detail (text changed by ' + added + ' characters)');
      return label + ' (+' + added + ' characters of detail)'; });
    await check('the opened item passes WCAG 2 A/AA (axe)', async () => { const v = await axe(); assert(v.length === 0, v.join(', ')); return 'no violations'; });
  } else { unsupported('an item opens with its detail and its next step', 'this build serves no sample cases: Today has nothing to open');
    unsupported('the opened item passes WCAG 2 A/AA (axe)', 'this build serves no sample cases: Today has nothing to open'); }
  await check('first workflow: a note is captured from the keyboard and shows up as captured', async () => {
    const note = 'acceptance note ' + Date.now() + ' call Dana about the invoice';
    const cap = p.getByPlaceholder(/Capture anything/i); assert(await cap.count(), 'no capture field');
    await cap.fill(note); await cap.press('Enter'); await p.waitForTimeout(2500);
    const t = await body(); assert(t.includes(note), 'the captured note is not shown'); return 'shown under "' + ((t.match(/Captured[^.]{0,30}/i) || ['captured'])[0]) + '"'; });
  if (sample) {
    await check('find narrows the list', async () => {
      const f = p.getByPlaceholder(/Find a customer or item/i); assert(await f.count(), 'no find field');
      await f.fill('Visa'); await p.waitForTimeout(900); const t = await body(); assert(/Visa documents/i.test(t), 'the matching item is not shown'); await f.fill(''); return 'query "Visa" shows the matching item'; });
  } else unsupported('find narrows the list', 'this build serves no sample cases: there is no list to narrow');
  await check('every main section opens without an error', async () => {
    const seen = [];
    for (const name of ['Work', 'Activity', 'Automations', 'Connections', 'Settings', 'Today']) {
      const b = p.getByRole('button', { name: new RegExp('^' + name, 'i') }).first(); if (!(await b.count())) continue;
      await b.click(); await p.waitForTimeout(700); const t = await body(); assert(t.length > 200 && !/something went wrong|unexpected error/i.test(t), name + ' did not render'); seen.push(name); }
    assert(seen.length >= 4, 'only ' + seen.join(',') + ' found'); return seen.join(', '); });
  // ---- Dashboards (Projects UI 0.3 and later). A build without the section records the checks as UNSUPPORTED. ----
  const dashButton = p.getByRole('button', { name: /^Dashboards/i }).first();
  if (!(await dashButton.count())) {
    for (const n of ['the Dashboards section opens and lists what this build can show', 'every offered dashboard opens with its sources named', 'a dashboard passes WCAG 2 A/AA (axe)', 'a dashboard does not scroll sideways in a phone-width window'])
      unsupported(n, 'this build has no Dashboards section');
  } else {
    // The catalogue is read from the page's own request (the session lives in the page, not in a cookie this script could reuse).
    let cat = {}; let offered = [];
    await check('the Dashboards section opens and lists what this build can show', async () => {
      const [resp] = await Promise.all([
        p.waitForResponse((r) => new URL(r.url()).pathname === '/api/dashboards' && r.status() === 200, { timeout: 20000 }),
        dashButton.click()]);
      cat = await resp.json(); offered = (cat.dashboards || []).map((d) => d.title);
      await p.waitForTimeout(1500); const t = await body();
      assert(offered.length >= 1, 'the catalogue offers no dashboard'); assert(!/something went wrong|unexpected error/i.test(t), 'the section shows an error');
      const missing = offered.filter((title) => !t.includes(title)); assert(missing.length === 0, 'offered by the API and not on the page: ' + missing.join(', ') + ' | page: ' + t.slice(0, 200));
      return offered.join(', '); });
    await check('every offered dashboard opens with its sources named', async () => {
      const seen = []; assert((cat.dashboards || []).length >= 1, 'no dashboard to open');
      for (const d of (cat.dashboards || [])) {
        const link = p.getByRole('button', { name: new RegExp(d.title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i') }).or(p.getByRole('link', { name: new RegExp(d.title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i') })).or(p.getByRole('tab', { name: new RegExp(d.title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i') })).first();
        assert(await link.count(), 'no control opens "' + d.title + '"'); await link.click(); await p.waitForTimeout(1200);
        const t = await body(); assert(!/something went wrong|unexpected error/i.test(t), d.title + ' shows an error');
        const src = (d.sources || []).map((x) => x.label).filter((l) => !t.includes(l)); assert(src.length === 0, d.title + ' does not name its source(s): ' + src.join(', '));
        seen.push(d.id); }
      return seen.join(', '); });
    await check('a dashboard passes WCAG 2 A/AA (axe)', async () => { const v = await axe(); assert(v.length === 0, v.join(', ')); return 'no violations'; });
    await check('a dashboard does not scroll sideways in a phone-width window', async () => {
      await p.setViewportSize({ width: 390, height: 800 }); await p.waitForTimeout(600);
      const m = await p.evaluate(() => ({ w: window.innerWidth, sw: document.documentElement.scrollWidth })); await p.setViewportSize({ width: 1280, height: 860 });
      assert(m.sw <= m.w + 1, 'the page is ' + m.sw + ' px wide in a ' + m.w + ' px window'); return '390 px'; });
    // ---- A journey with data. A fresh installation has no number to click: a test run is reported through the app's own intake, from
    // the page (so with the page's own session), and the person's path is then walked in the window: the QA dashboard appears, shows
    // the run, and the failed test is one click behind the "failed" number.
    const at = Date.now() / 1000; const runId = 'ui-journey-' + Math.round(at);
    const posted = await p.evaluate(async ([at, runId]) => {
      const r = await fetch('/api/dashboards/qa/results', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({
        project: 'main', suite: 'ui-journey', run_id: runId, platform: 'browser', environment: 'installed build', release: 'under-test', origin: 'ui_workflow', at,
        results: [{ test_id: 'uijourney.pass', status: 'passed', detail: 'reported by the acceptance run' }, { test_id: 'uijourney.fail', status: 'failed', detail: 'a deliberately failed row' }] }) });
      return r.status; }, [at, runId]).catch((e) => 'error ' + String(e).slice(0, 80));
    const journeys = ['after a test run is reported, the QA dashboard appears and shows it', 'the failed test is one click behind the failed number', 'the QA dashboard with data passes WCAG 2 A/AA (axe)'];
    if (posted === 404 || posted === 405 || posted === 501) for (const n of journeys) unsupported(n, 'this build does not accept reported test results (HTTP ' + posted + ')');
    else {
      await check(journeys[0], async () => {
        assert(posted === 200, 'reporting a run from the page answered ' + posted);
        const [resp] = await Promise.all([
          p.waitForResponse((r) => new URL(r.url()).pathname === '/api/dashboards' && r.status() === 200, { timeout: 20000 }),
          p.reload({ waitUntil: 'networkidle' }).then(async () => { const b = p.getByRole('button', { name: /^Dashboards/i }).first(); if (await b.count()) await b.click(); })]);
        const qa = ((await resp.json()).dashboards || []).find((d) => d.id === 'qa'); assert(qa, 'the catalogue still does not offer the QA dashboard');
        const rx = new RegExp(qa.title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');
        const link = p.getByRole('button', { name: rx }).or(p.getByRole('link', { name: rx })).or(p.getByRole('tab', { name: rx })).first();
        assert(await link.count(), 'no control opens "' + qa.title + '"');
        const [dresp] = await Promise.all([p.waitForResponse((r) => new URL(r.url()).pathname === '/api/dashboards/qa' && r.status() === 200, { timeout: 20000 }), link.click()]);
        const doc = await dresp.json(); const m = {}; for (const pnl of doc.panels || []) for (const x of pnl.metrics || []) m[x.id] = x.value;
        await p.waitForTimeout(1200); const t = await body();
        assert(m.failed >= 1 && m.planned >= 2, 'the dashboard does not count the reported run: ' + JSON.stringify({ planned: m.planned, failed: m.failed }));
        assert(/ui-journey/i.test(t), 'the page does not name the reported suite'); assert(/not ready|failed/i.test(t), 'the page does not say that a test failed');
        return qa.title + ': planned ' + m.planned + ', failed ' + m.failed + '; the suite is named on the page'; });
      await check(journeys[1], async () => {
        const failed = p.getByRole('button', { name: /failed/i }).first(); assert(await failed.count(), 'no control on the "failed" number');
        const [dr] = await Promise.all([p.waitForResponse((r) => new URL(r.url()).pathname === '/api/dashboards/qa/drill' && r.status() === 200, { timeout: 20000 }), failed.click()]);
        const recs = JSON.stringify(await dr.json()); await p.waitForTimeout(900); const t = await body();
        assert(recs.includes('uijourney.fail') && !recs.includes('uijourney.pass'), 'the records behind "failed" are not exactly the failed test');
        assert(t.includes('uijourney.fail'), 'the failed test is not shown on the page after the click'); return 'uijourney.fail listed; uijourney.pass not'; });
      await check(journeys[2], async () => { const v = await axe(); assert(v.length === 0, v.join(', ')); await p.keyboard.press('Escape'); return 'no violations'; });
    }
    const today = p.getByRole('button', { name: /^Today/i }).first(); if (await today.count()) { await today.click(); await p.waitForTimeout(600); }
  }
  await check('a phone-width window does not scroll sideways', async () => {
    await p.setViewportSize({ width: 390, height: 800 }); await p.waitForTimeout(500);
    const m = await p.evaluate(() => { const w = window.innerWidth, sw = document.documentElement.scrollWidth; const wide = [];
      if (sw > w + 1) for (const el of document.querySelectorAll('body *')) { const r = el.getBoundingClientRect(); if (r.width > 0 && r.right > w + 1 && el.children.length <= 1) wide.push(el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.split(' ')[0] : '') + ' right=' + Math.round(r.right) + ' "' + (el.innerText || '').replace(/\s+/g, ' ').slice(0, 30) + '"'); }
      return { w, sw, wide: wide.slice(0, 5), font: getComputedStyle(document.body).fontFamily.slice(0, 60) }; });
    await p.setViewportSize({ width: 1280, height: 860 });
    if (m.sw > m.w + 1 && process.env.KNOWN_NARROW_OVERFLOW === '1') { known.push('the page is ' + m.sw + ' px wide in a ' + m.w + ' px window (' + m.wide.slice(0, 2).join('; ') + ')'); return 'KNOWN DEFECT of this release, see below'; }
    assert(m.sw <= m.w + 1, 'the page is ' + m.sw + ' px wide in a ' + m.w + ' px window; widest elements: ' + m.wide.join('; ') + '; font ' + m.font); return '390 px'; });
  // A defect the release is known to have (set by the workflow for exactly that release) is recorded as KNOWN_DEFECT: not a pass, not a new failure.
  for (const k of known) { const r = results.find((x) => x.name.startsWith('a phone-width window')); if (r) { r.status = 'KNOWN_DEFECT'; r.detail = k; } console.log('KNOWN_DEFECT  ' + k); }
  await check('a second browser without the launch link gets no workspace and no data', async () => {
    const other = await browser.newContext(); const q = await other.newPage();
    const r = await q.request.get(origin + '/api/projects'); await q.goto(origin + '/', { waitUntil: 'domcontentloaded' }); await q.waitForTimeout(1200);
    const t = (await q.locator('body').innerText()).replace(/\s+/g, ' '); await other.close();
    assert(r.status() === 401, 'the API answered ' + r.status() + ' without a session'); assert(!/need your decision/i.test(t), 'the workspace rendered without a session'); return 'API 401; page shows: ' + t.slice(0, 80); });
  await check('no console error and no failed request during the journey', async () => { assert(consoleErrors.length === 0 && failed.length === 0, JSON.stringify({ consoleErrors: consoleErrors.slice(0, 3), failed: failed.slice(0, 5) })); return 'clean'; });
}
await browser.close();
try { proc.stdin.end(); } catch {}
await new Promise((r) => setTimeout(r, 2500)); if (proc.exitCode === null) proc.kill();
const summary = { pass: results.filter((r) => r.status === 'PASS').length, fail: results.filter((r) => r.status === 'FAIL').length, unsupported: results.filter((r) => r.status === 'UNSUPPORTED').length, known_defect: results.filter((r) => r.status === 'KNOWN_DEFECT').length, seconds: Math.round((Date.now() - t00) / 100) / 10 };
fs.writeFileSync(outFile, JSON.stringify({ platform: os.platform() + ' ' + os.release() + ' ' + os.arch(), sidecar: side, at: new Date().toISOString(), summary, results }, null, 1));
console.log(JSON.stringify(summary));
process.exit(summary.fail ? 1 : 0);
