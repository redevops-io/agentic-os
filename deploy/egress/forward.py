#!/usr/bin/env python3
"""Inbound port forwarder for services that live on an INTERNAL network.

Docker does not publish ports for a container attached only to an internal network, and attaching the container to a normal
network would give it open internet access. Instead the service stays internal and this forwarder (attached to both
networks) publishes ONE port and relays it to ONE fixed target: `--listen 0.0.0.0:3010 --to twenty:3000`.
It never opens any other destination, so it adds an inbound path and no outbound one. Standard library only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

IDLE_S = 300.0
MAX_CONNECTIONS = 512


async def _pump(src, dst, idle_s):
    try:
        while True:
            chunk = await asyncio.wait_for(src.read(65536), idle_s)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            dst.close()
        except Exception:  # noqa: BLE001
            pass


def make_handler(target_host: str, target_port: int, idle_s: float = IDLE_S):
    sem = asyncio.Semaphore(MAX_CONNECTIONS)

    async def handle(reader, writer):
        async with sem:
            try:
                ur, uw = await asyncio.wait_for(asyncio.open_connection(target_host, target_port), 10)
            except (OSError, asyncio.TimeoutError):
                writer.close()
                return
            await asyncio.gather(_pump(reader, uw, idle_s), _pump(ur, writer, idle_s))
    return handle


async def serve(listen_host: str, listen_port: int, target_host: str, target_port: int, idle_s: float = IDLE_S):
    return await asyncio.start_server(make_handler(target_host, target_port, idle_s), listen_host, listen_port)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="inbound forwarder to one fixed target")
    ap.add_argument("--listen", required=True)
    ap.add_argument("--to", required=True, help="host:port of the only destination")
    a = ap.parse_args(argv)
    lh, _, lp = a.listen.rpartition(":")
    th, _, tp = a.to.rpartition(":")
    if not (lp.isdigit() and tp.isdigit() and th):
        print("--listen and --to must be host:port", file=sys.stderr)
        return 2

    async def run():
        server = await serve(lh or "0.0.0.0", int(lp), th, int(tp))
        print(json.dumps({"ts": round(time.time(), 3), "event": "forwarding", "listen": a.listen, "to": a.to}), flush=True)
        async with server:
            await server.serve_forever()
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
