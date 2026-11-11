#!/bin/bash
# Build a SLES for SAP 15 SP7 qcow2 template for Apache CloudStack (KVM) from the SLE Full ISO.
#
# Usage: build.sh <SLE-15-SP7-Full-x86_64-GM-Media1.iso> <output_dir> [disk_size_gb] [format]
#   format: qcow2 (CloudStack KVM, default) or vhd (CloudStack XenServer / XCP-ng)
#   For VMware, convert the qcow2 to OVA/VMDK with the VMware tooling of your choice.
#
# Steps:
#   1. Unattended AutoYaST install into a temporary libvirt domain (no network, ISO repos only)
#   2. Offline customisation: cloud-init CloudStack config, remove ISO repos
#   3. virt-sysprep (machine-id, SSH host keys, logs, cloud-init state) and compressed qcow2 export
set -euo pipefail

ISO="${1:?ISO path required}"
OUT_DIR="${2:?output directory required}"
DISK_GB="${3:-20}"
FORMAT="${4:-qcow2}"
case "${FORMAT}" in qcow2|vhd) ;; *) echo "Unsupported format: ${FORMAT}" >&2; exit 1;; esac
NAME="sles4sap15sp7-cs-build"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DISK="${OUT_DIR}/${NAME}.qcow2"
if [ "${FORMAT}" = "vhd" ]; then FINAL="${OUT_DIR}/sles4sap-15-sp7-cloudstack-xen.vhd"; else FINAL="${OUT_DIR}/sles4sap-15-sp7-cloudstack-kvm.qcow2"; fi
export LIBVIRT_DEFAULT_URI="qemu:///system"
export LIBGUESTFS_BACKEND=direct

mkdir -p "${OUT_DIR}"
if virsh dominfo "${NAME}" >/dev/null 2>&1; then
  echo "Domain ${NAME} already exists, remove it first" >&2
  exit 1
fi

qemu-img create -f qcow2 "${WORK_DISK}" "${DISK_GB}G"

echo "== AutoYaST installation (serial console output follows)"
virt-install \
  --name "${NAME}" \
  --memory 4096 --vcpus 2 \
  --osinfo detect=on,require=off \
  --disk "path=${WORK_DISK},format=qcow2,bus=virtio" \
  --location "${ISO}" \
  --initrd-inject "${SCRIPT_DIR}/autoinst.xml" \
  --extra-args "autoyast=file:///autoinst.xml console=ttyS0,115200 textmode=1 net.ifnames=0" \
  --network none \
  --graphics none \
  --serial "file,path=${OUT_DIR}/install-console.log" \
  --autoconsole none \
  --noreboot \
  --wait -1

virsh undefine "${NAME}" >/dev/null

echo "== Offline customisation"
virt-customize -a "${WORK_DISK}" \
  --copy-in "${SCRIPT_DIR}/90_cloudstack.cfg:/etc/cloud/cloud.cfg.d/" \
  --run-command 'chmod 0644 /etc/cloud/cloud.cfg.d/90_cloudstack.cfg' \
  --run-command 'zypper --non-interactive removerepo --all || true' \
  --run-command 'systemctl enable sshd chronyd cloud-init-local cloud-init cloud-config cloud-final' \
  --run-command 'rm -f /etc/udev/rules.d/70-persistent-net.rules' \
  --run-command 'for v in NETCONFIG_DNS_POLICY NETCONFIG_NTP_POLICY; do grep -q "^${v}=" /etc/sysconfig/network/config && sed -i "s/^${v}=.*/${v}=\"auto\"/" /etc/sysconfig/network/config || echo "${v}=\"auto\"" >> /etc/sysconfig/network/config; done' \
  --run-command 'rm -f /var/lib/YaST2/runme_at_boot /var/lib/YaST2/reconfig_system; systemctl disable YaST2-Second-Stage.service YaST2-Firstboot.service autoyast-initscripts.service 2>/dev/null || true' \
  --run-command 'cloud-init clean --logs || true'

echo "== Sysprep"
virt-sysprep -a "${WORK_DISK}" \
  --operations defaults,-ssh-userdir,-user-account

# virt-customize/virt-sysprep may leave a generated machine-id; clones must generate their own
# (wicked DHCP client-id / DUID are derived from it, duplicates cause address conflicts)
guestfish --rw -a "${WORK_DISK}" -i <<'GF'
truncate /etc/machine-id
rm-f /var/lib/dbus/machine-id
rm-rf /var/lib/wicked
mkdir-p /var/lib/wicked
GF

echo "== Export compressed template"
if [ "${FORMAT}" = "vhd" ]; then
  # XenServer/XCP-ng: dynamic VHD; SLES kernel includes Xen PV block/net drivers
  qemu-img convert -O vpc -o subformat=dynamic "${WORK_DISK}" "${FINAL}"
else
  qemu-img convert -c -O qcow2 "${WORK_DISK}" "${FINAL}"
fi
rm -f "${WORK_DISK}"
sha256sum "${FINAL}" | tee "${FINAL}.sha256"
qemu-img info "${FINAL}"
