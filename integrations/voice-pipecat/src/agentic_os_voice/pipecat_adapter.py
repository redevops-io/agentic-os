"""Pipecat adapter: expose the bridge's allowlist as Pipecat tools, and route distress language to a person.

Pipecat (``pipecat-ai``) owns the conversation: audio frames, turn detection, interruptions, the LLM context and, if
used, Pipecat Flows' node transitions. This module does not reimplement any of that. It contributes three small things:

* :func:`tool_schemas` / :func:`register_governed_tools`: Pipecat function-call schemas generated from the bridge's
  :class:`~agentic_os_voice.contracts.ToolSpec` allowlist, and handlers that hold ONLY an
  :class:`~agentic_os_voice.bridge.ActionProxy`;
* :func:`governed_flow_function`: the same handler shaped as a Pipecat Flows function, which returns the next node;
* :class:`DistressGuard`: a frame processor that keeps distress and emergency transcripts away from the model.

Importing this module imports ``pipecat``; the rest of the package does not.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Iterable, Optional

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import Frame, InterruptionFrame, TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams, LLMService

from .bridge import ActionProxy
from .contracts import ActionResult, ActionStatus, ConfirmationState, ToolSpec
from .handoff import HANDOFF_SCRIPT, classify

_log = logging.getLogger("agentic_os_voice.pipecat")

#: an extra boolean the model can set on a tool that needs the caller's agreement. It is evidence of consent that is
#: recorded in the receipt; it is never authority (a model can set it without asking, which is why consequential
#: actions also need a human approval at the gateway).
CONFIRM_FIELD = "caller_confirmed"


def function_schema(spec: ToolSpec) -> FunctionSchema:
    js = spec.json_schema()
    props = dict(js["properties"])
    if spec.requires_confirmation:
        props[CONFIRM_FIELD] = {"type": "boolean", "description": "true only if the caller has just agreed to this exact action"}
    required = list(js["required"]) + ([CONFIRM_FIELD] if spec.requires_confirmation else [])
    return FunctionSchema(name=spec.name, description=spec.description or spec.name, properties=props, required=required)


def tool_schemas(tools: Dict[str, ToolSpec], names: Optional[Iterable[str]] = None) -> ToolsSchema:
    chosen = [tools[n] for n in (names if names is not None else tools) if n in tools]
    return ToolsSchema(standard_tools=[function_schema(s) for s in chosen])


def _split(args: Dict[str, Any]) -> tuple:
    a = dict(args)
    flag = a.pop(CONFIRM_FIELD, None)
    return a, (ConfirmationState.CONFIRMED if flag is True else ConfirmationState.DECLINED if flag is False else ConfirmationState.NOT_REQUESTED)


def governed_handler(proxy: ActionProxy, tool_name: str) -> Callable[[FunctionCallParams], Any]:
    async def handler(params: FunctionCallParams) -> None:
        args, confirmation = _split(dict(params.arguments))
        # A barge-in or a framework timeout cancels THIS coroutine (asyncio.CancelledError at the await below). The bridge's
        # work is shielded from that: see VoiceActionBridge.submit_async. CancelledError is not caught here, so Pipecat
        # settles the call as cancelled; the action's receipt is read back with proxy.status() or by asking again.
        result = await proxy.call(tool_name, args, confirmation=confirmation)
        await params.result_callback(result.as_tool_result())
    handler.__name__ = f"governed_{tool_name}"
    return handler


def register_governed_tools(llm: LLMService, proxy: ActionProxy, names: Optional[Iterable[str]] = None, *,
                            catch_all: bool = True) -> None:
    """Register one handler per allowlisted tool. Reads are cancelled by an interruption (they have no effect);
    writes are registered as asynchronous calls (``cancel_on_interruption=False``): the caller talking over the
    agent does not abandon them. With ``catch_all`` a call to ANY other name is routed to the bridge too, which refuses
    it as not allowlisted and journals the attempt (otherwise the framework would drop it silently)."""
    specs = proxy.tool_specs
    for n in (names if names is not None else specs):
        spec = specs[n]
        llm.register_function(n, governed_handler(proxy, n), cancel_on_interruption=not spec.side_effecting,
                              timeout_secs=(spec.timeout_s or (10.0 if spec.side_effecting else 5.0)) + 5.0)
    if catch_all:
        async def unknown(params: FunctionCallParams) -> None:
            await governed_handler(proxy, str(params.function_name))(params)
        llm.register_function(None, unknown, cancel_on_interruption=True, timeout_secs=10.0)


def governed_flow_function(proxy: ActionProxy, spec: ToolSpec, *, next_node: Callable[[ActionResult], Any] = lambda r: None):
    """A Pipecat Flows function for an allowlisted tool. Flows owns the conversation state: ``next_node`` maps the
    result to the node to enter (or None to stay). The side effect, its authorisation and its receipt stay in the bridge."""
    from pipecat.flows import FlowsFunctionSchema   # imported late: Flows is optional for users of the plain handlers

    async def handler(args: Dict[str, Any], flow_manager: Any):
        a, confirmation = _split(dict(args))
        result = await proxy.call(spec.name, a, confirmation=confirmation)
        return result.as_tool_result(), next_node(result)

    fs = function_schema(spec)
    return FlowsFunctionSchema(name=spec.name, description=fs.description, properties=fs.properties, required=fs.required,
                               handler=handler, cancel_on_interruption=not spec.side_effecting)


class DistressGuard(FrameProcessor):
    """Place it between speech-to-text and the user aggregator. A transcript that matches the distress screen is NOT passed
    on: the model does not get a turn for it. The guard speaks a fixed script that requests a handoff and promises nothing,
    latches the session through the bridge (so no further automated tool can run) and calls ``on_handoff``."""

    def __init__(self, proxy: ActionProxy, *, on_handoff: Optional[Callable[[str, ActionResult], Any]] = None, **kwargs):
        super().__init__(**kwargs)
        self._proxy = proxy
        self._on_handoff = on_handoff
        self.latched: Optional[str] = None
        self.handoff_result: Optional[ActionResult] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            category = None if self.latched else classify(frame.text)
            if self.latched or category:
                if category:
                    self.latched = category
                    await self.push_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
                    await self.push_frame(TTSSpeakFrame(HANDOFF_SCRIPT), FrameDirection.DOWNSTREAM)
                    # shielded inside the bridge: a second interruption cannot abandon the request
                    self.handoff_result = await self._proxy.request_handoff(category)
                    if self._on_handoff:
                        r = self._on_handoff(category, self.handoff_result)
                        if asyncio.iscoroutine(r):
                            await r
                return          # swallowed: no model turn for distress text, and none after the latch
        await self.push_frame(frame, direction)
