"""Portability layer: the few operating-system facts the governance core depends on, in one place (standard library only).

``locks``    inter-process exclusive file locks (flock / LockFileEx)
``files``    private files and directories, atomic replacement
``process``  a liveness probe that never signals the process

Linux behaviour is the behaviour the callers had before; macOS keeps POSIX mode bits; Windows is fail-closed where it
cannot show a property (see ``files``). Nothing here claims isolation or sandboxing.
"""
from . import files, locks, process

__all__ = ["files", "locks", "process"]
