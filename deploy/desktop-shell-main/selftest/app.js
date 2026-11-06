(async () => { const t = (e) => (e ? e.textContent.replace(/\s+/g, " ").trim() : ""); await new Promise((r) => setTimeout(r, 600));
  return { page: "app", title: document.title, status: t(document.getElementById("status")), role: document.getElementById("status")?.getAttribute("role"), url: document.getElementById("url")?.value }; })()
