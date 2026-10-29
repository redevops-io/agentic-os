# Authorization sweep

`sweep.py` drives all 17 gated apps through six modes (unprovisioned, read-only profile, write profile, foreign principal, deliberately standalone, nothing configured) and three
start-up refusals (invalid switch value, unusable permissions store, enterprise requested with the package absent) over four surfaces: every route, `Operator.invoke`, the same
capability over HTTP `/invoke`, and the policy lookup with hostile tool-name variants. The expected outcome comes from the trusted table `agentic_os/tool_actions.py`, not from the apps.
A denied call must make no outbound connection, DNS lookup or process launch (a sentinel refuses and records them).

    python tests/sweeps/sweep.py                          # quick (~22,000 calls, ~20 s)
    python tests/sweeps/sweep.py --fuzz 130 --seed 7      # the full sweep (~86,000 calls, ~20 s)
    python tests/sweeps/sweep.py --app books --json out.json

Run it with `APPROVAL_SECRET` unset. It needs no network and does not touch a live system or the checkout's runtime state. `apps/test_acceptance_sweep.py` runs it in CI at a small fuzz and
proves the harness can fail (a gate that lets one write tool through, accepts a near-miss name, skips the operator check, leaks a network call, or starts on a bad switch is reported).
