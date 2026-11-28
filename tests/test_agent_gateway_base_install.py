"""The gateway and token issuing work on a base install of the kernel: nothing on those paths may import an optional
package. Argument validation once imported ``agentic_os.products``, which needs ``httpx``, and every gateway call failed
on a base install. This runs in a subprocess where the optional packages cannot be imported."""
import subprocess
import sys
import textwrap

BLOCKED = ("httpx", "fastapi", "starlette", "uvicorn", "pydantic")


def test_calls_are_validated_and_tokens_issued_without_any_optional_package(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in {BLOCKED!r}:
                    raise ModuleNotFoundError(f"No module named {{name!r}} (blocked: not in a base install)", name=name)
        sys.meta_path.insert(0, Block())
        from agentic_os.agent_gateway import AgentGateway, CapabilityRegistry, DevTokenVerifier, HandlerResult, McpGatewayBridge
        from agentic_os.agent_gateway import agent_tokens
        from agentic_os.agent_gateway.contracts import CapabilityKind, CapabilityManifest
        reg = CapabilityRegistry()
        reg.register(CapabilityManifest("find.read", "f", CapabilityKind.DIRECT, permissions=("find.read",),
                     input_schema={{"type": "object", "properties": {{"q": {{"type": "string"}}}}}}), lambda r, e: HandlerResult(ok=True, output=[]))
        v = DevTokenVerifier()
        t = v.issue("a", tenant="t", grants=("find.read",))
        bridge = McpGatewayBridge(AgentGateway(registry=reg, authorize=v.authorize), v)
        assert bridge.call_tool(t, "find.read", {{"q": "x"}})["status"] == "ok"
        assert bridge.call_tool(t, "find.read", {{"q": 1}})["error"].startswith("invalid_arguments")
        token = agent_tokens.issue(r"{tmp_path / 'agents.json'}", "a1", ["projects.read"], days=1, authority_db=r"{tmp_path / 'a.db'}")
        assert token.startswith("aga_")
        print("ok")
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-1500:]
