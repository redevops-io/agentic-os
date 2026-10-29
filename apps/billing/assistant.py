"""The app's conversational panel: deterministic tool routing + a GOVERNED grounded Q&A fallback.

This replaces the previous third-party console object (which carried its own model client and so bypassed the
governed router). Here the ONLY model call is ``agentic_os.app.llm.governed_text`` with explicit data
classifications; when the router refuses or no in-boundary endpoint is configured it fails closed to a
deterministic answer from the primer. Tool calls made from this panel are NEVER approved: side-effecting
tools only stage a preview (the gate lives in ``core``), and nothing here can obtain the operator marker.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable

from agentic_os.app_gate import default_authorizer, permissions_requested

_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[[dict], dict]
    keywords: tuple = ()
    side_effecting: bool = False
    params: dict = field(default_factory=dict)


def _tokens(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def governed_answer(prompt: str, classifications, *, max_tokens: int = 400, temperature: float = 0.2):
    """Text from the governed in-boundary model, or None (fail closed: no external fallback)."""
    try:
        from agentic_os.app.llm import governed_text
    except Exception:  # noqa: BLE001
        return None
    try:
        return governed_text(prompt, classifications=tuple(classifications), max_tokens=max_tokens,
                             temperature=temperature)
    except Exception:  # noqa: BLE001
        return None


class Assistant:
    def __init__(self, title: str, primer: str, tools: list, suggestions: list, subtitle: str,
                 classifications: tuple, authorizer=None):
        # ``authorizer(principal, spec, args) -> None | deny-reason`` is the SAME seam the harness ToolRegistry uses
        # (``permissions.make_authorizer(..., tool_actions=agentic_os.tool_actions.tool_actions("<app>"))``). When set, every tool call is
        # authorised before its body runs, and an authorizer that raises is a denial. None means the process-wide default authorizer (the
        # enterprise gate), and no gate at all only when permissions were not requested.
        self.authorizer = authorizer
        self.title, self.primer, self.tools = title, primer, list(tools)
        self.suggestions, self.subtitle, self.classifications = list(suggestions), subtitle, tuple(classifications)

    def _pick(self, message: str):
        q = _tokens(message)
        best, score = None, 0
        for t in self.tools:
            s = sum(2 for k in t.keywords if _tokens(k) and _tokens(k) <= q) + len(_tokens(t.name.replace("_", " ")) & q)
            if s > score:
                best, score = t, s
        return best if score >= 2 else None

    def _primer_answer(self, message: str) -> str:
        q = _tokens(message) - {"the", "a", "an", "is", "what", "how", "do", "i", "to", "of", "and", "my"}
        paras = [p.strip() for p in self.primer.split("\n\n") if p.strip()]
        ranked = sorted(paras, key=lambda p: -len(q & _tokens(p)))
        return ranked[0] if ranked and q & _tokens(ranked[0]) else \
            "I can answer questions about this module or run one of its tools: " + ", ".join(t.name for t in self.tools) + "."

    def _deny(self, tool, principal, message: str):
        """The deny reason for this call, or None to allow. The authorizer is this console's own, else the process-wide default the enterprise
        bootstrap installs (read at call time: the SAME seam as every harness ``ToolRegistry``). No authorizer at all is the basic default
        (None) unless permissions were requested, and then the console is closed: it is never an open door beside a gated app (RR-02).
        An authorizer that raises is a denial."""
        authorizer = self.authorizer if self.authorizer is not None else default_authorizer()
        if authorizer is None:
            return "permissions were requested (CR_PERMISSIONS / CR_IDENTITY_*) but no authorizer is installed; denied (fail closed)" \
                if permissions_requested() else None
        try:
            return authorizer(principal, SimpleNamespace(name=tool.name, side_effecting=tool.side_effecting, approval_required=False),
                              {"message": message})
        except Exception as e:  # noqa: BLE001 — fail closed
            return f"authorizer error ({type(e).__name__})"

    def respond(self, message: str, principal=None) -> dict:
        message = (message or "").strip()
        if not message:
            return {"intent": "help", "text": self.subtitle, "evidence": []}
        tool = self._pick(message)
        if tool is not None:
            deny = self._deny(tool, principal, message)
            if deny:
                return {"intent": tool.name, "text": f"[blocked] {tool.name}: {deny}", "evidence": [], "denied": True}
        if tool is not None:
            try:
                out = tool.fn({"message": message})
            except Exception as e:  # noqa: BLE001
                return {"intent": tool.name, "text": f"{tool.name} failed: {e}", "evidence": []}
            return {"intent": tool.name, "text": out.get("text", ""), "evidence": [],
                    "data": out.get("data"), "side_effecting": tool.side_effecting}
        grounded = self._primer_answer(message)
        text = governed_answer(
            "Answer the user's question using ONLY this reference. Be concise.\n\nREFERENCE:\n"
            f"{self.primer}\n\nQUESTION: {message}", self.classifications) or grounded
        return {"intent": "answer", "text": text, "evidence": []}

    def panel_html(self, prefix: str = "asst") -> str:
        sugg = "".join(f"<button type='button' class='btn' data-q='{html.escape(s, quote=True)}'>{html.escape(s)}</button> "
                       for s in self.suggestions)
        return (
            f"<div class='card' id='{prefix}-panel'><div class='card__head'><h2 class='card__title'>"
            f"{html.escape(self.title)}</h2></div><div class='body-m'>{html.escape(self.subtitle)}</div>"
            f"<div>{sugg}</div><div id='{prefix}-out' class='body-m' style='white-space:pre-wrap'></div>"
            f"<form id='{prefix}-form'><input id='{prefix}-in' class='btn' style='min-width:60%' "
            "placeholder='Ask…'><button class='btn' type='submit'>Send</button></form></div>"
            "<script>(function(){var p=" + repr(prefix) + ";var f=document.getElementById(p+'-form'),"
            "i=document.getElementById(p+'-in'),o=document.getElementById(p+'-out');"
            "function ask(q){if(!q)return;o.textContent='…';fetch('api/agent',{method:'POST',headers:"
            "{'Content-Type':'application/json'},body:JSON.stringify({message:q})}).then(function(r){return r.json()})"
            ".then(function(d){o.textContent=d.text||''}).catch(function(){o.textContent='(assistant unavailable)'})}"
            "f.addEventListener('submit',function(e){e.preventDefault();ask(i.value)});"
            "document.querySelectorAll('#'+p+'-panel [data-q]').forEach(function(b){b.addEventListener('click',"
            "function(){ask(b.getAttribute('data-q'))})})})();</script>"
        )
