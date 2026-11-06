(async () => {
  /*LIB*/
  const before = await observe();
  const input = q("#capture-input");
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  set.call(input, "Jordan still hasn't sent the passport scan");
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await wait(() => !q('form.capture button[type="submit"]').disabled, 5000, "capture button");
  q('form.capture button[type="submit"]').click();
  const linked = await wait(() => qa(".banner.ok").map(txt).find((t) => /Linked to/.test(t)), 15000, "capture to be linked");
  await wait(() => /Jordan Lee/.test(txt(q("aside.detail h2"))), 15000, "the linked case to open");
  const evidence = qa("aside.detail li").map(txt).filter((t) => /Your note/.test(t));
  const approve = await wait(() => qa("aside.detail button").find((b) => /Approve & send/.test(txt(b)) && !b.disabled), 10000, "approve button");
  const preSend = txt(q("aside.detail")).match(/Nothing has been sent yet|Drafted — nothing has been sent/) ? "nothing sent yet" : "unclear";
  approve.click();
  await wait(() => qa(".banner.ok").map(txt).some((t) => /Approved and sent/.test(t)), 15000, "send to be confirmed");
  const lifecycle = qa('ol[aria-label="Progress of this action"] li').map((li) => (li.className.includes("on") ? "[x] " : "[ ] ") + txt(li).replace(/ — (done|not yet)$/, ""));
  const after = await observe();
  return { before: { headline: before.headline, needsDecision: before.needsDecision, jordan: before.jordan }, capture: { linked, evidence, preSend },
           lifecycle, after: { headline: after.headline, needsDecision: after.needsDecision, waiting: after.waiting, jordan: after.jordan }, env: before };
})()
