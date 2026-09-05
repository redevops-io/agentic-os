#!/usr/bin/env bash
# Build the autounattend ISO and start a hands-off Windows 11 install on Proxmox VM 9001.
# Run ON the PVE host (or: ssh proxmox 'bash -s' < build-and-run.sh). Idempotent-ish; re-runnable.
#
# Makes the install DRIVER-FREE by switching the VM to a SATA disk (native AHCI) + e1000 NIC
# (native driver) — so the unattended install needs no VirtIO injection. The VirtIO CD stays
# attached only so first-logon can install the QEMU guest agent (a plain MSI).
set -euo pipefail

VMID="${VMID:-9001}"
ISO_DIR="${ISO_DIR:-/var/lib/vz/template/iso}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="${TEMPLATE:-$HERE/autounattend.xml}"

echo "== hands-off Win11 install on VM $VMID =="
[ -f "$ISO_DIR/Win11.iso" ] || { echo "ERROR: $ISO_DIR/Win11.iso missing"; exit 1; }
[ -f "$ISO_DIR/virtio-win.iso" ] || echo "WARN: virtio-win.iso missing (guest agent won't install)"

# 1) generated local-admin password (ephemeral; kept root-only on the host, never printed)
PW="Rdo-$(openssl rand -hex 6)!"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/iso"
sed "s|@@PASSWORD@@|$PW|g" "$TEMPLATE" > "$WORK/iso/autounattend.xml"
umask 077; echo "vm=$VMID user=tester pass=$PW" > /root/rdo-win11-9001.cred; echo "  (local-admin cred → /root/rdo-win11-9001.cred, root-only)"

# 2) build the autounattend ISO (label AUTOUNATTEND so Setup finds it on any drive)
if command -v xorrisofs >/dev/null 2>&1; then
  xorrisofs -quiet -o "$ISO_DIR/autounattend.iso" -V AUTOUNATTEND -J -r "$WORK/iso"
elif command -v genisoimage >/dev/null 2>&1; then
  genisoimage -quiet -o "$ISO_DIR/autounattend.iso" -V AUTOUNATTEND -J -r "$WORK/iso"
else
  apt-get install -y xorriso >/dev/null 2>&1
  xorrisofs -quiet -o "$ISO_DIR/autounattend.iso" -V AUTOUNATTEND -J -r "$WORK/iso"
fi
echo "  built $ISO_DIR/autounattend.iso"

# 3) driver-free VM: move the boot disk to SATA + NIC to e1000
qm stop "$VMID" 2>/dev/null || true
DISK="$(qm config "$VMID" | sed -n 's/^scsi0: \([^,]*\).*/\1/p')"
if [ -n "$DISK" ]; then
  echo "  moving $DISK  scsi0 → sata0"
  qm set "$VMID" --delete scsi0 >/dev/null
  qm set "$VMID" --sata0 "${DISK},discard=on,ssd=1" >/dev/null
fi
qm set "$VMID" --net0 "e1000,bridge=vmbr0" >/dev/null
# CDs: Win11 (ide2), VirtIO for the guest agent (ide3), autounattend (sata1)
qm set "$VMID" --sata1 "local:iso/autounattend.iso,media=cdrom" >/dev/null
qm set "$VMID" --boot "order=sata0;ide2" >/dev/null
echo "  VM reconfigured (SATA disk + e1000 + autounattend CD)"

# 4) start + satisfy "Press any key to boot from CD" headlessly via sendkey
qm start "$VMID"
echo "  started; sending boot keypresses…"
for t in 2 3 4 6 9 13; do sleep "$t"; qm sendkey "$VMID" ret >/dev/null 2>&1 || true; qm sendkey "$VMID" spc >/dev/null 2>&1 || true; done
echo "== install underway. Poll for the guest agent (install done + first logon + qemu-ga):"
echo "   ssh proxmox 'while ! qm agent $VMID ping 2>/dev/null; do sleep 30; done; echo AGENT_UP'"
