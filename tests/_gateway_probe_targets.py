"""Synthetic ``sandbox.execute`` targets for the gateway tests. They run inside the confined child, so they must import with the standard library only."""
import os


def env_probe(inputs):
    return {"env_keys": sorted(os.environ), "has_ambient_secret": "GATEWAY_TEST_AMBIENT_SECRET" in os.environ}


def raises_with_detail(inputs):
    raise RuntimeError("db at /srv/internal/prod.sqlite refused: password=SYNTH-PW-123")
