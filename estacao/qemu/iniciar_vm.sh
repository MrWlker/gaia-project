#!/usr/bin/env bash
# Sobe o "Pi 3" emulado: Cortex-A53, 4 nucleos, 1 GB, com a D435 passada pelo USB.
#
#     ./iniciar_vm.sh                  # console na tela; sair: Ctrl-a x
#     ./iniciar_vm.sh --fundo          # em segundo plano; console em vm/console.log
#
# Portas no notebook:
#     ssh -p 2222 pi@localhost         (senha gaia)
#     http://localhost:8080            interface da estacao
# De dentro da VM, o notebook e 10.0.2.2 - e ja esta na lista de servidores
# do estacao.json, entao a mesma configuracao vale para a VM e para o Pi.
#
# A CAMERA NO USB
# ---------------
# O QEMU precisa abrir /dev/bus/usb/... como o seu usuario. Uma vez, no notebook:
#     sudo cp 99-realsense-qemu.rules /etc/udev/rules.d/ && sudo udevadm control --reload
# e replugue a camera. O dispositivo e declarado por vendor:product, entao
# quando a librealsense reseta a camera e ela reenumera, o QEMU a reencontra.
# Se o notebook tiver a camera aberta (realsense-viewer, outro processo), a VM
# nao a recebe.
set -euo pipefail
cd "$(dirname "$0")"
DIR=${GAIA_VM_DIR:-$PWD/vm}
[ -f "$DIR/gaia-pi.qcow2" ] || { echo "rode ./preparar_vm.sh antes"; exit 1; }

if lsusb | grep -qiE '8086:(0b07|0b3a)'; then
  lsusb | grep -iE '8086:(0b07|0b3a)'
  dev=$(lsusb | grep -iE '8086:(0b07|0b3a)' | head -1 | awk '{print "/dev/bus/usb/"$2"/"substr($4,1,3)}')
  [ -w "$dev" ] || echo "AVISO: sem permissao em $dev - veja 99-realsense-qemu.rules no topo deste script"
else
  echo "D435 nao esta no USB do notebook; a estacao vai subir em modo simulado"
fi

TELA=(-nographic)
[ "${1:-}" = --fundo ] && TELA=(-display none -serial "file:$DIR/console.log" -daemonize -pidfile "$DIR/qemu.pid")

exec qemu-system-aarch64 \
  -M virt -cpu cortex-a53 -smp 4 -m 1024 \
  -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
  -drive if=virtio,format=qcow2,file="$DIR/gaia-pi.qcow2" \
  -drive if=virtio,format=raw,readonly=on,file="$DIR/seed.iso" \
  -netdev user,id=rede,hostfwd=tcp::2222-:22,hostfwd=tcp::8080-:8080 \
  -device virtio-net-pci,netdev=rede \
  -device qemu-xhci,id=xhci \
  -device usb-host,bus=xhci.0,vendorid=0x8086,productid=0x0b07 \
  -device usb-host,bus=xhci.0,vendorid=0x8086,productid=0x0b3a \
  "${TELA[@]}"
