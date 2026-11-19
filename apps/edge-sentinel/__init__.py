"""edge-sentinel — a SOC dashboard on a real CrowdSec core, with a human in the loop before any block.

This is the package view (P1): the same modules the monorepo loads through the `apps/edge-sentinel`
namespace, installed properly so nothing shadows the standard library and the quickstart is one command.
Keep this file import-light: importing `edge_sentinel` must not import FastAPI, uvicorn, the kernel, or
any app module — `edge-sentinel --help` runs in a bare venv, and the dashboard loads what it needs.
"""

__version__ = "0.1.0"