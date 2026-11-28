#!/usr/bin/env python3
"""A local agent on any OpenAI-compatible endpoint (for example a self-hosted Qwen) that operates a rAIls deployment
through the MCP bridge. No hosted service is involved: the model endpoint, the bridge and the deployment can all be on
your own machines.

    RAILS_AGENTS_DIR=... RAILS_TOKEN_FILE=... python rails_agent.py --base-url http://HOST:PORT/v1 --model NAME "which projects exist?"

The agent has exactly the tools the bridge lists for its credential, and each call goes through the deployment's
Governed Agent Gateway. This file holds no credential and no business state; it is a thin loop:
model -> tool call -> bridge -> gateway -> model. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

BRIDGE = Path(__file__).resolve().parents[1] / "mcp-bridge" / "server" / "index.js"


class McpStdio:
    """The smallest MCP client: newline-delimited JSON-RPC over a child process's standard streams."""

    def __init__(self, command: List[str], env: Dict[str, str]) -> None:
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self._id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "rails-local-agent", "version": "0.1.0"}})
        self.notify("notifications/initialized")

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, **({"params": params} if params is not None else {})}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the MCP bridge closed its output")
        msg = json.loads(line)
        if "error" in msg:
            raise RuntimeError(str(msg["error"].get("message", "MCP error")))
        return msg["result"]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def chat(base_url: str, model: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], *, api_key: str = "dummy", extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = {"model": model, "messages": messages, "tools": tools, "temperature": 0, "max_tokens": 1200, **(extra or {})}
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["choices"][0]["message"]


#: How much of one tool result is given back to the model. The first version cut every result at 8,000 characters, silently, in the middle of
#: the JSON (a busy dashboard is 17,000 to 25,000): a model that cannot see the end of a document it was given asks again with other filters,
#: and one of three runs used its whole step budget doing so (acceptance/edge-sentinel/step-limit). A cut is now always SAID to the model.
RESULT_CHARS = int(os.environ.get("RAILS_AGENT_RESULT_CHARS", "24000") or "24000")


def run(question: str, *, mcp: McpStdio, complete: Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Dict[str, Any]], max_steps: int = 6,
        result_chars: Optional[int] = None) -> Dict[str, Any]:
    """One question. Returns the answer and the transcript of tool calls (name, arguments, whether the gateway refused)."""
    tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", "")[:900], "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}}
             for t in mcp.request("tools/list")["tools"]]
    names = {t["function"]["name"] for t in tools}
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": "You operate a business application through the tools provided. Use a tool for every fact; never invent data. "
                                      "If a tool reports a denial or a pending approval, say so to the person and stop: do not look for another route."},
        {"role": "user", "content": question}]
    calls: List[Dict[str, Any]] = []
    for _ in range(max_steps):
        msg = complete(messages, tools)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return {"answer": (msg.get("content") or "").strip(), "calls": calls, "tools_offered": sorted(names)}
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
        for tc in tool_calls:
            name = (tc.get("function") or {}).get("name", "")
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            if name not in names:                       # a tool the credential is not offered is never forwarded
                text, refused = json.dumps({"status": "denied", "reason": "this agent is not offered that tool"}), True
            else:
                res = mcp.request("tools/call", {"name": name, "arguments": args if isinstance(args, dict) else {}})
                text, refused = (res.get("content") or [{}])[0].get("text", ""), bool(res.get("isError"))
            limit = RESULT_CHARS if result_chars is None else int(result_chars)
            calls.append({"tool": name, "arguments": args, "refused": refused, "result_chars": len(text), "given_to_model_chars": min(len(text), limit), "cut": len(text) > limit})
            shown = text[:limit] if len(text) <= limit else text[:limit] + f"\n[RESULT CUT: {limit} of {len(text)} characters are shown. Asking again with the same or other filters returns the same result; answer from what is shown, and say that it is incomplete.]"
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": shown})
    return {"answer": "", "calls": calls, "tools_offered": sorted(names), "stopped": "step limit reached"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("question")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--no-thinking", action="store_true", help="send chat_template_kwargs.enable_thinking=false (Qwen on vLLM)")
    a = ap.parse_args(argv)
    # SYSTEMROOT, WINDIR, TEMP and TMP: Node on Windows cannot open a network connection without SYSTEMROOT in its environment.
    env = {k: os.environ[k] for k in ("PATH", "RAILS_URL", "RAILS_TOKEN", "RAILS_AGENTS_DIR", "RAILS_TOKEN_FILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ}
    mcp = McpStdio(["node", str(BRIDGE)], env)
    try:
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if a.no_thinking else None
        out = run(a.question, mcp=mcp, complete=lambda m, t: chat(a.base_url, a.model, m, t, api_key=os.environ.get("OPENAI_API_KEY", "dummy"), extra=extra))
    finally:
        mcp.close()
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
