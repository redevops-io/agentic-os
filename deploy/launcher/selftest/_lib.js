// (concatenated in front of observe.js / act.js by the runner — no modules, so it works as a bare expression in a webview)
const wait = async (fn, ms = 20000, what = "condition") => {
  const t0 = Date.now();
  for (;;) { try { const v = fn(); if (v) return v; } catch (_) { /* not yet */ } if (Date.now() - t0 > ms) throw new Error("timed out waiting for " + what); await new Promise((r) => setTimeout(r, 100)); }
};
const txt = (e) => (e ? e.textContent.replace(/\s+/g, " ").trim() : "");
const q = (s, r = document) => r.querySelector(s), qa = (s, r = document) => [...r.querySelectorAll(s)];
const observe = async () => {
  await wait(() => /need.* your decision|Nothing needs you/.test(txt(q("h1.h"))) || q(".connbar") || /Couldn't load|isn't responding|didn't accept|out of date/.test(txt(q("section.content"))), 25000, "Today to render");
  const rowsIn = (title) => { const card = qa(".card").find((c) => txt(q(".hd .pill", c)) === title); return card ? qa(".trow .t", card).map(txt) : []; };
  const jordan = qa(".trow").find((r) => /Jordan Lee/.test(txt(r)));
  const group = jordan && qa(".card").find((c) => c.contains(jordan));
  return {
    title: document.title,
    headline: txt(q("h1.h")),
    nav: qa('nav[aria-label="Primary"] button').map((b) => b.getAttribute("aria-label")),
    groups: qa(".card > .hd .pill").map(txt),
    needsDecision: rowsIn("Needs your decision"),
    waiting: rowsIn("Waiting on others"),
    jordan: jordan ? { group: txt(q(".hd .pill", group)), line: txt(q(".s", jordan)) } : null,
    banner: txt(q(".connbar")) || null,
    sampleBanner: /Sample data/.test(document.body.innerText),
    place: { hash: location.hash, last: localStorage.getItem("redevops.last-place") },
    session: localStorage.getItem("redevops.today.session"),
  };
};
