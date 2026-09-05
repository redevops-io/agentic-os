# Hands-off Windows 11 install (Proxmox VM 9001)

The deterministic, agent-free path to the P0.1 acceptance VM: Windows 11 installs itself, creates
a local admin, autologons once, and installs the **QEMU guest agent** — after which the host drives
everything (Python, repo, tests) via `qm guest exec`. No noVNC-canvas clicking, no browser agent.

## Why driver-free
`build-and-run.sh` switches VM 9001 to a **SATA disk** (native AHCI) and an **e1000 NIC** (native
driver), so the unattended install needs **no VirtIO driver injection** — the single biggest cause
of unattended-install failures. The VirtIO CD stays attached only so first-logon can install the
guest agent (a plain MSI). Swap to VirtIO later for performance if desired.

## Run
```bash
# on the PVE host (or: ssh proxmox 'bash -s' < build-and-run.sh)
bash deploy/launcher/autounattend/build-and-run.sh
# then wait for the guest agent (install done + first logon + qemu-ga):
ssh proxmox 'while ! qm agent 9001 ping 2>/dev/null; do sleep 30; done; echo AGENT_UP'
# then run the tests inside Windows from the host:
qm guest exec 9001 -- cmd /c ver           # smoke: guest exec works
```
`autounattend.xml` is a **template** — `@@PASSWORD@@` is filled with a generated value at build
time (kept root-only at `/root/rdo-win11-9001.cred`, never committed). The local-admin password is
not needed for `qm guest exec` (the agent runs as SYSTEM); it exists only for the interactive login.

## Honest status
Written to a known-good pattern but **not validated end-to-end here** (blind headless install; I
can't see the setup screen). Win 11 25H2's OOBE is strict — if the guest agent never comes up, open
the console once to see where setup stopped. The `build-and-run.sh` sends boot keypresses via
`qm sendkey` to get past "Press any key to boot from CD" without a console.
