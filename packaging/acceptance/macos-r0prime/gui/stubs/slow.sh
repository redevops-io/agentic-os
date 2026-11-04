#!/bin/bash
# Delay, then become the real sidecar (same args/env/pipes): a slow start (cold disk, antivirus) simulation.
sleep "${RDO_SLOW_SECONDS:-5}"; exec "$RDO_REAL_SIDECAR" "$@"
