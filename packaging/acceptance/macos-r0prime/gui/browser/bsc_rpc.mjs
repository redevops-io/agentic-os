// Tiny JSON-lines RPC around headless Chromium (playwright-core), so python scenarios can drive a
// controlled browser with a disposable profile. env: PWCORE, CHROMIUM. One JSON request per stdin line.
//  {op:"launch", profile, args?:[...]}  {op:"newpage"}  {op:"goto", page, url, wait?}  {op:"reload", page}
//  {op:"eval", page, expr}  {op:"text", page}  {op:"url", page}  {op:"closectx"}  {op:"quit"}
import readline from 'readline';
const pw = (await import(process.env.PWCORE)).default;
let ctx = null; const pages = [];
const send = (o) => process.stdout.write(JSON.stringify(o) + '\n');
const rl = readline.createInterface({ input: process.stdin });
for await (const line of rl) {
  let r; try { r = JSON.parse(line); } catch (e) { send({ error: 'bad json' }); continue; }
  try {
    if (r.op === 'launch') {
      ctx = await pw.chromium.launchPersistentContext(r.profile, { executablePath: process.env.CHROMIUM, headless: true, args: r.args || [] });
      pages.length = 0; pages.push(ctx.pages()[0] || await ctx.newPage()); send({ ok: true, pages: pages.length });
    } else if (r.op === 'newpage') { pages.push(await ctx.newPage()); send({ ok: true, page: pages.length - 1 });
    } else if (r.op === 'goto') {
      const p = pages[r.page ?? 0]; let status = null, err = null;
      try { const resp = await p.goto(r.url, { waitUntil: r.wait || 'load', timeout: 15000 }); status = resp ? resp.status() : null; } catch (e) { err = String(e).split('\n')[0].slice(0, 200); }
      if (r.settle) await p.waitForTimeout(r.settle);
      send({ ok: true, status, err, url: p.url().replace(/#lc=[^&]*/, '#lc=<code>') });
    } else if (r.op === 'waiturl') {
      const p = pages[r.page ?? 0]; let ok = true; try { await p.waitForURL(new RegExp(r.re), { timeout: r.timeout || 20000 }); } catch (e) { ok = false; }
      await p.waitForTimeout(r.settle ?? 1500); send({ ok, url: p.url().replace(/#lc=[^&]*/, '#lc=<code>') });
    } else if (r.op === 'reload') { const p = pages[r.page ?? 0]; await p.reload(); await p.waitForTimeout(r.settle ?? 1500); send({ ok: true });
    } else if (r.op === 'eval') { const p = pages[r.page ?? 0]; const v = await p.evaluate(r.expr); send({ ok: true, value: v });
    } else if (r.op === 'text') { const p = pages[r.page ?? 0]; send({ ok: true, text: (await p.evaluate(() => document.body ? document.body.innerText : '')).slice(0, 600) });
    } else if (r.op === 'banner') { const p = pages[r.page ?? 0]; send({ ok: true, banner: await p.evaluate(() => { const a = document.querySelector('[role=alert]'); return a ? a.innerText : null; }), title: await p.title() });
    } else if (r.op === 'url') { send({ ok: true, url: pages[r.page ?? 0].url().replace(/#lc=[^&]*/, '#lc=<code>') });
    } else if (r.op === 'closectx') { await ctx.close(); ctx = null; send({ ok: true });
    } else if (r.op === 'quit') { if (ctx) await ctx.close(); send({ ok: true }); process.exit(0);
    } else send({ error: 'unknown op' });
  } catch (e) { send({ error: String(e).split('\n')[0].slice(0, 300) }); }
}
