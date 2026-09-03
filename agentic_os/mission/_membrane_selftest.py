"""Importable capabilities used only by the membrane self-tests.

They run *inside* the contained child, so they exercise the boundary from within:
proving the child cannot see ambient secrets, and that resource ceilings actually bite.
"""
import os


def echo(inputs):
    """Baseline: a well-behaved capability returns its result."""
    return {"echo": inputs}


def read_ambient_secret(inputs):
    """A misbehaving/curious agent trying to read a process credential. Under containment
    the environment is scrubbed, so it sees nothing."""
    return {"seen": os.environ.get("MEMBRANE_TEST_SECRET", "")}


def busy(inputs):
    """Exceed the CPU/duration ceiling — should be killed by the membrane, not run free."""
    x = 0
    while True:
        x += 1


def alloc(inputs):
    """Exceed the address-space ceiling."""
    blob = bytearray(int(inputs.get("mb", 4096)) * 1024 * 1024)
    return {"len": len(blob)}
