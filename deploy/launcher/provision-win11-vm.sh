#!/usr/bin/env bash
# Provision a Windows 11 acceptance VM on the Proxmox (PVE) host for P0.1 launcher testing.
#
# Run ON the PVE host (or: ssh proxmox 'bash -s' < provision-win11-vm.sh). It creates a VM with
# the settings Windows 11 requires — q35 + UEFI (OVMF) + TPM 2.0 + Secure Boot, VirtIO SCSI/NET —
# auto-downloads the VirtIO driver ISO, and attaches both ISOs so Windows setup can load the
# VirtIO storage/network drivers. The Windows 11 ISO itself is NOT auto-fetched (Microsoft serves
# it via an expiring dynamic link); drop it in the ISO dir first (see below).
#
# Deliberately provisioned like a CUSTOMER machine — no dev tools — so the launcher's Edge
# Sentinel prerequisite detection (WSL2 / Docker / virtualization / credential store) is exercised.
# After install, snapshot to pristine and re-run the installer per ACCEPTANCE.md.
set -euo pipefail

VMID="${VMID:-9001}"
NAME="${NAME:-redevops-win11-accept}"
CORES="${CORES:-4}"
RAM_MB="${RAM_MB:-8192}"
DISK_GB="${DISK_GB:-64}"
DISK_STORE="${DISK_STORE:-local-lvm}"      # lvmthin for the VM disk (pvesm status)
ISO_STORE="${ISO_STORE:-local}"            # dir storage for ISOs
ISO_DIR="${ISO_DIR:-/var/lib/vz/template/iso}"
WIN_ISO="${WIN_ISO:-Win11.iso}"            # place the Windows 11 ISO here as this name
VIRTIO_ISO="virtio-win.iso"
VIRTIO_URL="https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"

echo "== ReDevOps Win11 acceptance VM: id=$VMID name=$NAME ($CORES vCPU / ${RAM_MB}MB / ${DISK_GB}G) =="

command -v qm >/dev/null || { echo "ERROR: not a PVE host (no qm)"; exit 1; }
if qm status "$VMID" >/dev/null 2>&1; then
  echo "VM $VMID already exists. Destroy with: qm destroy $VMID --purge   (aborting)"; exit 1
fi

# 1) VirtIO drivers (stable, auto)
if [ ! -f "$ISO_DIR/$VIRTIO_ISO" ]; then
  echo "-- downloading VirtIO drivers ISO"
  curl -fSL "$VIRTIO_URL" -o "$ISO_DIR/$VIRTIO_ISO"
else
  echo "-- VirtIO ISO present"
fi

# 2) Windows 11 ISO (manual — Microsoft link expires)
if [ ! -f "$ISO_DIR/$WIN_ISO" ]; then
  cat <<EOF
!! Windows 11 ISO not found at $ISO_DIR/$WIN_ISO
   Download it (any machine) from https://www.microsoft.com/software-download/windows11
   then place/scp it as: $ISO_DIR/$WIN_ISO
   Creating the VM WITHOUT the install ISO attached; attach it later with:
     qm set $VMID --ide2 $ISO_STORE:iso/$WIN_ISO,media=cdrom
     qm set $VMID --boot order='ide2;scsi0'
EOF
  HAVE_WIN=0
else
  echo "-- Windows 11 ISO present"; HAVE_WIN=1
fi

# 3) Create the VM — Windows 11 requirements: q35, OVMF/UEFI, TPM 2.0, Secure Boot, VirtIO.
echo "-- creating VM $VMID"
qm create "$VMID" \
  --name "$NAME" --ostype win11 \
  --machine q35 --bios ovmf --cpu host --cores "$CORES" --sockets 1 --memory "$RAM_MB" \
  --scsihw virtio-scsi-single \
  --net0 "virtio,bridge=vmbr0" \
  --efidisk0 "$DISK_STORE:0,efitype=4m,pre-enrolled-keys=1" \
  --tpmstate0 "$DISK_STORE:1,version=v2.0" \
  --scsi0 "$DISK_STORE:$DISK_GB,discard=on,ssd=1" \
  --ide3 "$ISO_STORE:iso/$VIRTIO_ISO,media=cdrom" \
  --agent enabled=1

if [ "$HAVE_WIN" = "1" ]; then
  qm set "$VMID" --ide2 "$ISO_STORE:iso/$WIN_ISO,media=cdrom"
  qm set "$VMID" --boot order='ide2;scsi0'
fi

cat <<EOF

== VM $VMID created. Next (interactive — needs the PVE web console) ==
  1. Open the console (PVE web UI → $VMID → Console) and start the VM.
  2. Install Windows 11. When no disk shows, "Load driver" from the VirtIO CD:
        amd64\\w11  (vioscsi) for the disk, then NetKVM for networking.
  3. At OOBE, create a LOCAL account — treat it as a customer machine (no dev tools).
  4. Snapshot pristine:  qm snapshot $VMID win11-clean
  5. Test the launcher installer per deploy/launcher/ACCEPTANCE.md, re-running from snapshots:
        qm rollback $VMID win11-clean
  Suggested snapshots: win11-clean, win11-wsl2-only, win11-docker-installed,
                       win11-no-virtualization, win11-existing-redevops, win11-broken-install
EOF
