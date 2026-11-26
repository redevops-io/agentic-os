"""Governed writes to the operational applications (ERPNext, Chatwoot, Twenty) for external agents.

An agent PROPOSES a write. It becomes a one-node mission in the Mission Runtime, bound to that capability and those exact
inputs, and parks at the runtime's human gate. A person of the same tenant approves what they were shown; the runtime then
performs it once and the application is read back. No second approval system: the gate, the record of who approved, the
inputs digest and the expiry are the runtime's own.
"""
