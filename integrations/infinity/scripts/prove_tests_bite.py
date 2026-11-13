"""Show that the security tests fail when the protection they cover is removed.

For each mutation this script disables one protection in a scratch COPY of the package, runs the tests that cover it, and requires them to fail;
then it runs the same tests on the untouched copy and requires them to pass. Nothing in the working tree is modified.

    python scripts/prove_tests_bite.py            # all mutations
    python scripts/prove_tests_bite.py -k replay  # those whose name contains 'replay'
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
SRC = "src/redevops_infinity/"
E = "tests/test_endpoints_negative.py"

MUTATIONS = [
    ("signature-check", [(SRC + "callback_auth.py", "key_id, ts, nonce, body)\n        if not hmac.compare_digest(expected, sig.lower()):", "key_id, ts, nonce, body)\n        if False:")], [E + "::test_a_bad_signature_is_refused"]),
    ("inbound-nonce-replay", [(SRC + "callback_auth.py", "if not self.nonces.check_and_add(key_id, nonce):\n            raise AuthError(401, \"replayed_or_unrecordable_nonce\")", "if False:\n            raise AuthError(401, \"x\")")], [E + "::test_a_replayed_request_is_refused"]),
    ("clock-skew", [(SRC + "callback_auth.py", "raise AuthError(401, \"key_expired\")\n        if abs(now - ts) > self.skew_s:", "raise AuthError(401, \"key_expired\")\n        if False:")], [E + "::test_clock_skew_is_refused_both_ways"]),
    ("audience", [(SRC + "callback_auth.py", "if aud and aud != self.audience:", "if False:")], [E + "::test_the_signature_binds_method_path_body_and_audience"]),
    ("scope-check", [(SRC + "callback_auth.py", "if scope is None or scope not in peer.scopes:", "if scope is None:")], [E + "::test_a_peer_without_the_scope_gets_403"]),
    ("revoked-key", [(SRC + "callback_auth.py", "if peer is None or peer.revoked:", "if peer is None:")], [E + "::test_revoked_and_expired_keys_are_refused"]),
    ("rate-limit", [(SRC + "http_server.py", "if not self.limiter.allow(\"ip:\" + remote, self.ip_rate, self.ip_burst):", "if False:")], [E + "::test_requests_are_rate_limited_per_source_and_per_peer"]),
    ("body-limit", [(SRC + "http_server.py", "if n < 0 or n > MAX_BODY_BYTES:", "if n < 0:")], [E + "::test_bodies_are_bounded_and_must_be_json"]),
    ("callback-allowlist", [(SRC + "callback_auth.py", "entry = next((o for o in origins if o.normalized == origin), None)", "entry = origins[0] if origins else None")], ["tests/test_callback_policy.py::test_loopback_policy_refuses"]),
    ("callback-private-address", [(SRC + "callback_auth.py", "ok = _public(ip) or entry.allow_private or", "ok = True or")], ["tests/test_callback_policy.py::test_a_public_name_must_resolve_only_to_public_addresses"]),
    ("callback-redirects", [(SRC + "callback_auth.py", "return PostOutcome(\"refused\", st, \"redirect_not_followed\")", "return PostOutcome(\"delivered\", st)")], ["tests/test_callback_policy.py::test_redirects_are_not_followed"]),
    ("callback-signature-binds-tenant", [(SRC + "callback_auth.py", "if g(H_TENANT) != self.expect_tenant:", "if False:")], ["tests/test_callback_policy.py::test_callback_replay_tamper_substitution_and_expiry_are_refused"]),
    ("callback-event-dedup", [(SRC + "callback_auth.py", "                return \"duplicate\", extra", "                return \"ok\", extra")], ["tests/test_callback_policy.py::test_a_callback_verifies_and_a_duplicate_is_acknowledged_not_applied"]),
    ("invocation-id-reuse", [(SRC + "store.py", "same = ex[\"operation\"] == row[\"operation\"] and ex[\"args_sha256\"] == row[\"args_sha256\"] and ex[\"key_id\"] == row[\"key_id\"]", "same = True")], ["tests/test_idempotency_and_isolation.py::test_the_same_id_with_different_arguments_is_refused"]),
    ("replay-entitlement", [(SRC + "rap_gateway.py", "if not self._entitled(peer, auth, spec, args):", "if False:")], ["tests/test_authority_and_approval.py::test_a_revoked_permission_denies_new_calls_and_replays"]),
    ("unknown-outcome-recovery", [(SRC + "rap_gateway.py", "if state in (\"none\", \"reserved\", \"released\"):", "if True:")], ["tests/test_crash_boundaries.py::test_a_crash_inside_the_handler_is_outcome_unknown_and_is_never_sent_again",
                                                                                                                      "tests/test_crash_boundaries.py::test_a_reservation_that_never_left_is_run_again_but_one_that_left_is_not"]),
    ("completed-needs-a-receipt", [(SRC + "rap_gateway.py", "            if not ref:\n                self._decide(row, D, ActionStatus.OUTCOME_UNKNOWN, Reason.NO_DOWNSTREAM_RECEIPT)", "            if False:\n                self._decide(row, D, ActionStatus.OUTCOME_UNKNOWN, Reason.NO_DOWNSTREAM_RECEIPT)")], ["tests/test_outcome_unknown.py::test_success_without_a_receipt_is_not_completed"]),
    ("stable-event-id", [(SRC + "rap_gateway.py", "headers = callback_headers(peer, url=row[\"callback_url\"], body=body, event_id=d[\"event_id\"],", "headers = callback_headers(peer, url=row[\"callback_url\"], body=body, event_id=\"evt_\" + uuid.uuid4().hex,")], ["tests/test_crash_boundaries.py::test_a_crash_after_posting_the_result_and_before_marking_it_delivered_delivers_it_once"]),
    ("approval-gate", [(SRC + "rap_gateway.py", "        if st is GatewayStatus.PENDING_APPROVAL:\n            held", "        if False:\n            held")], ["tests/test_authority_and_approval.py::test_a_consequential_action_waits_for_a_named_person_and_runs_once",
                                                                                                                  "tests/test_crash_boundaries.py::test_a_crash_cannot_bypass_approval"]),
    ("cancel-ownership", [(SRC + "rap_gateway.py", "if row is None or row[\"key_id\"] != peer.key_id or row[\"tenant\"] != peer.tenant:\n            return", "if row is None:\n            return")], ["tests/test_idempotency_and_isolation.py::test_another_principal_of_the_same_tenant_cannot_cancel_a_call"]),
    ("no-abandon-after-dispatch", [(SRC + "rap_gateway.py", "        elif row[\"state\"] == \"dispatching\":\n            self.store.update(key, cancel_requested=1)", "        elif row[\"state\"] == \"dispatching\":\n            self.store.update(key, cancel_requested=1, state=\"decided\", status=\"cancelled\")")], ["tests/test_cancellation_timeouts_subscriptions.py::test_cancel_after_dispatch_never_abandons_a_committed_write"]),
    ("allowlist", [(SRC + "rap_gateway.py", "allowed = spec is not None and (peer.operations is None or spec.name in peer.operations)", "allowed = spec is not None")], ["tests/test_authority_and_approval.py::test_an_operation_outside_the_peers_allowlist_is_refused"]),
    ("child-scope", [(SRC + "lineage.py", "if op.local or not op.child_allowed:", "if op.local:"), (SRC + "rap_gateway.py", "if refusal is None and not spec.child_allowed and self._is_child(peer, inv.group_id):", "if False:")],
     ["tests/test_lineage.py::test_the_child_can_only_use_operations_the_owner_marked_child_allowed"]),
    ("lineage-immutable", [(SRC + "lineage.py", "if tuple(json.loads(have[\"ancestors_json\"])) != anc:", "if False:")], ["tests/test_lineage.py::test_the_lineage_is_recorded_once_and_cannot_be_rewritten"]),
    ("child-lifetime", [(SRC + "lineage.py", "ttl = min(self.policy.ttl_s, remaining - 1.0)", "ttl = self.policy.ttl_s")], ["tests/test_lineage.py::test_a_child_never_outlives_its_parent"]),
    ("no-children-without-spawner", [(SRC + "lineage.py", "if self.spawner is None:\n            raise LineageRefused(\"children_not_enabled\", Reason.SPAWN_REFUSED)", "if False:\n            pass")], ["tests/test_lineage.py::test_without_a_spawner_children_are_refused_not_given_the_parents_authority"]),
    ("unknown-field-refusal", [(SRC + "contracts.py", "    if extra:\n        raise WireError(\"invalid_request\", \"unknown field(s)\")", "    if False:\n        pass")], ["tests/test_wire_contracts.py::test_malformed_invocations_are_refused", "tests/test_authority_and_approval.py::test_no_rap_message_can_approve"]),
]


def run(tests, cwd):
    env = {k: v for k, v in os.environ.items() if k not in ("APPROVAL_SECRET", "AGENTIC_OS_APPROVAL_SECRET")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(Path(cwd) / "src")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests], cwd=cwd, env=env, capture_output=True, text=True)
    return r.returncode == 0, r.stdout[-300:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="")
    a = ap.parse_args()
    bad = 0
    for name, edits, tests in MUTATIONS:
        if a.k and a.k not in name:
            continue
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "pkg"
            shutil.copytree(PKG, work, ignore=shutil.ignore_patterns("harness", "__pycache__", ".pytest_cache", "*.egg-info"))
            ok_before, _ = run(tests, work)
            for path, old, new in edits:
                f = work / path
                text = f.read_text(encoding="utf-8")
                if text.count(old) != 1:
                    print(f"{name}: MUTATION DOES NOT APPLY ({path})")
                    bad += 1
                    break
                f.write_text(text.replace(old, new), encoding="utf-8")
            else:
                ok_after, tail = run(tests, work)
                verdict = "bites" if (ok_before and not ok_after) else "DOES NOT BITE"
                bad += verdict != "bites"
                print(f"{name:34s} unmodified: {'pass' if ok_before else 'FAIL'}   protection removed: {'pass' if ok_after else 'fail'}   -> {verdict}")
    print("RESULT", "all mutations are caught" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
