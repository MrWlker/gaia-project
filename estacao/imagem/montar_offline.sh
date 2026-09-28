#!/usr/bin/env bash
# Baixa, NO NOTEBOOK, tudo que a estacao precisa no Pi: wheels aarch64 para
# Python 3.9 (bullseye). O Pi instala sem rede nenhuma (instalar_pi.sh).
#
#     ./montar_offline.sh        # -> cache/wheels/ (~45 MB)
#
# Versoes fixadas onde a mais nova ja nao tem wheel cp39: numpy 1.26 e
# Pillow 10.4 sao as ultimas com Python 3.9. manylinux_2_28 exige glibc 2.28;
# o bullseye tem 2.31.
set -euo pipefail
cd "$(dirname "$0")"
W=cache/wheels
mkdir -p "$W"
PLAT=(--platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 --platform manylinux_2_28_aarch64 --platform any)
baixar() { pip download "$@" "${PLAT[@]}" --python-version 3.9 --implementation cp --abi cp39 --abi none \
             --only-binary=:all: --no-deps -d "$W" -q; }
baixar "numpy==1.26.4" "pillow==10.4.0" pyrealsense2
baixar requests urllib3 idna certifi charset-normalizer
# DHT: adafruit_dht -> blinka -> platformdetect/pureio/typing; pyftdi/pyusb/pyserial
# sao dependencias declaradas do blinka. sysv_ipc e RPi.GPIO so existem como
# fonte - o RPi.GPIO ja vem no Pi OS Lite, e sem sysv_ipc o DHT usa bit-bang.
baixar adafruit-circuitpython-dht adafruit-blinka adafruit-platformdetect adafruit-pureio \
  adafruit-circuitpython-typing typing-extensions pyftdi pyusb pyserial binho-host-adapter toml
# biblioteca nativa: o pyrealsense2 abre a camera por libusb-1.0. O Pi OS Lite
# costuma trazer, a imagem minima do Debian nao - e "costuma" nao serve para
# instalacao offline. Vai a do bullseye arm64, extraida do .deb.
N=cache/nativas
if [ ! -e "$N/libusb-1.0.so.0" ]; then
  mkdir -p "$N" cache/debs
  curl -s --fail -o cache/debs/libusb.deb \
    http://deb.debian.org/debian/pool/main/libu/libusb-1.0/libusb-1.0-0_1.0.24-3_arm64.deb
  x=$(mktemp -d); dpkg-deb -x cache/debs/libusb.deb "$x"
  cp -a "$x"/usr/lib/aarch64-linux-gnu/libusb-1.0.so* "$N"/; rm -rf "$x"
fi
ls "$W" "$N"
du -sh "$W" "$N"
