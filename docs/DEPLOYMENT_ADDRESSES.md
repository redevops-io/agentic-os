# Deployment addresses and private topology

The repository carries no address plan, host alias, home path or personal mailbox of the machines it is deployed or
tested on. Where a script or a test needs one, it reads it from the environment (or from a gitignored local file) and
stops with a clear message when it is missing. Documentation and examples use the reserved documentation values:
`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` (RFC 5737), `example.internal`, `*.example`, and `REPLACE_ME`
where a deployment must supply a value.

## Configuration points

| What | Variable | Default |
|---|---|---|
| Model endpoint for the Sidekick fallback | `SIDEKICK_MODEL_BASE_URL` | unset (no fallback model) |
| Governed model endpoint | `REDEVOPS_LLM_BASE_URL` | unset |
| Public-demo deploy scripts (`deploy/*/deploy.sh`): SSH address of the target host | `AD_HOST`, `AT_HOST`, `EV_HOST`, `PERM_HOST`, `RC_HOST`, `SEC_HOST`, `U_HOST`, `WC_HOST` (one per script) | none: the script stops |
| Directory on the target host | the same name with `_DIR`, for example `AD_HOST_DIR` | `/opt/agentic-os/<service>` |
| Checkout of the `runtime-contracts` package staged into a build context | `RUNTIME_CONTRACTS` | none: the script stops |
| Where the unsubscribe ledger persists on the target | `U_DATA_DIR` (deploy script), `UNSUB_DATA_DIR` (compose) | `<host dir>/data` |
| Integration tests against a remote Docker host (`tests/test_installer_remote_docker.py`, `tests/test_event_store_postgres.py`) | `RDO_REMOTE_DOCKER_IT=1`, `RDO_REMOTE_DOCKER_SSH`, `RDO_REMOTE_DOCKER_HOST` | skipped; with `RDO_REMOTE_DOCKER_IT=1` and a variable unset, the test fails naming it |
| Vault KV mount for the platform secrets bundle | `REDEVOPS_VAULT_MOUNT` (alias `VAULT_MOUNT`) | none: with `VAULT_ADDR` + `VAULT_TOKEN` set and no mount, every Vault operation stops with an error naming the variable |
| Document-editor integration tests: how the container reaches the host | `DOCEDITOR_IT_HOST_ADDR` | the Docker bridge gateway |

A default local install needs none of these: services bind to `127.0.0.1` and the model endpoint is `localhost`.

## Local configuration file for the deploy scripts

```bash
cp deploy/deploy.local.env.example deploy/deploy.local.env   # gitignored
$EDITOR deploy/deploy.local.env                              # replace every REPLACE_ME
deploy/admin-console/deploy.sh
```

`deploy/lib/deploy-env.sh` loads the file, and `deploy_require NAME "what"` refuses to continue while a value is empty
or still `REPLACE_ME`. The ingress hint that each script prints at the end names the host you configured.

## The guard

`tests/test_no_private_topology.py` runs `scripts/check_private_topology.py` over every tracked text file. It fails on:

* a private-range address (RFC 1918, link-local, the RFC 6598 shared address space, IPv6 unique-local and link-local)
  that is not in `scripts/private_topology_allowlist.json`;
* a development host alias, an `ssh` alias for the hypervisor, a home or scratch-session path, a private build-host
  directory, a MAC address, a `.lan`/`.internal`/`.corp`/`.ts.net` host name or a personal-provider mailbox that is not
  allow-listed;
* an allow-list entry that is a wildcard, has no written reason, names a file that no longer contains the literal, or
  belongs to a category that can never be listed.

The allow-list holds, per file and per literal, the synthetic fixtures that network-guard code and its tests need
(they must classify private addresses, so they contain some) and the names of hypervisor products in the deployment
skills. Do not widen it to make a check pass: replace the value with a documentation value or a configuration point.

```bash
python scripts/check_private_topology.py              # list violations (exit 1)
python scripts/check_private_topology.py --inventory  # every finding, with counts per category
```

Git history keeps whatever was committed earlier; the guard governs the tree.
