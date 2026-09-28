#!/usr/bin/env bash
# Prepara a VM que faz o papel do Raspberry Pi 3 do GAIA.
#
#     ./preparar_vm.sh          # uma vez; baixa ~300 MB
#     ./iniciar_vm.sh           # toda vez
#
# POR QUE `-M virt` E NAO `-M raspi3b`
# ------------------------------------
# O QEMU tem uma maquina raspi3b, mas nela a rede e o USB sao o DWC2 emulado:
# lento, so USB 2, e o passthrough de uma camera isocronica como a D435 por ali
# nao funciona na pratica. O que importa reproduzir do Pi 3 nao e a placa, e o
# ORCAMENTO: Cortex-A53, 4 nucleos, 1 GB, arm64. A `virt` com `-cpu cortex-a53
# -m 1024` reproduz isso e ainda tem xHCI para passar a camera.
#
# POR QUE DEBIAN 11 (BULLSEYE)
# ----------------------------
# pyrealsense2 so publica wheel aarch64 para Python 3.8 e 3.9. Bullseye tem 3.9;
# bookworm tem 3.11 e obrigaria a compilar a librealsense - horas num A53. O Pi
# de verdade vai com "Raspberry Pi OS (Legacy) Lite 64-bit", que e bullseye: a
# mesma base, os mesmos pacotes, o mesmo Python.
set -euo pipefail
cd "$(dirname "$0")"
DIR=${GAIA_VM_DIR:-$PWD/vm}
mkdir -p "$DIR"
URL=https://cloud.debian.org/images/cloud/bullseye/latest/debian-11-generic-arm64.qcow2

for bin in qemu-system-aarch64 qemu-img genisoimage; do
  command -v $bin >/dev/null || { echo "falta $bin:  sudo apt install -y qemu-system-arm qemu-efi-aarch64 qemu-utils genisoimage"; exit 1; }
done
[ -f /usr/share/qemu-efi-aarch64/QEMU_EFI.fd ] || { echo "falta o UEFI:  sudo apt install -y qemu-efi-aarch64"; exit 1; }

if [ ! -f "$DIR/base.qcow2" ]; then
  echo "baixando Debian 11 arm64..."
  curl -L --fail -o "$DIR/base.qcow2.part" "$URL"
  mv "$DIR/base.qcow2.part" "$DIR/base.qcow2"
fi

# o disco da VM e uma camada sobre a base: estragou, apaga gaia-pi.qcow2 e recomeca
if [ ! -f "$DIR/gaia-pi.qcow2" ]; then
  qemu-img create -f qcow2 -F qcow2 -b "$DIR/base.qcow2" "$DIR/gaia-pi.qcow2" 16G
fi

CHAVE=$(cat ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub 2>/dev/null | head -1 || true)
[ -n "$CHAVE" ] || echo "aviso: sem chave em ~/.ssh; entre com pi/gaia"

mkdir -p "$DIR/seed"
cat > "$DIR/seed/meta-data" <<META
instance-id: gaia-pi-1
local-hostname: gaia-pi
META
cat > "$DIR/seed/user-data" <<USER
#cloud-config
hostname: gaia-pi
users:
  - name: pi
    groups: [sudo, plugdev, video, dialout]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    plain_text_passwd: gaia
$( [ -n "$CHAVE" ] && echo "    ssh_authorized_keys: [\"$CHAVE\"]" || true)
ssh_pwauth: true
timezone: America/Sao_Paulo
package_update: true
packages: [python3-venv, python3-pip, python3-numpy, python3-pil, python3-requests, usbutils, rsync]
# o Pi 3 tem 1 GB e nenhum swap por padrao no cloud image; o Pi OS cria 100 MB
swap:
  filename: /swapfile
  size: 512M
USER
genisoimage -quiet -output "$DIR/seed.iso" -volid cidata -joliet -rock "$DIR/seed/user-data" "$DIR/seed/meta-data"
echo "pronto em $DIR. Agora: ./iniciar_vm.sh  (o primeiro boot demora: e ARM emulado)"
