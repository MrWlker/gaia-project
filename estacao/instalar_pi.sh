#!/usr/bin/env bash
# Instala a estacao no Pi 3 (Raspberry Pi OS Lite 64-bit, bullseye) ou na VM
# do QEMU - 100% OFFLINE. Roda NO Pi, dentro da pasta gaia/.
#
# SEM APT, SEM VENV, SEM PIP
# --------------------------
# Tudo que a estacao precisa vem em wheels/ (aarch64, cp39), baixado no
# notebook. Wheel e um zip: e extraido em lib/ e o Python do sistema o acha
# por PYTHONPATH. Assim a instalacao nao depende de rede nenhuma - e nao
# depende do repositorio do bullseye, que saiu de suporte em 2026-08 e ja
# serve indices apontando para .deb que nao existem mais (404).
#
# Do sistema so se usa o python3 (3.9) e, no Pi de verdade, o RPi.GPIO que
# ja vem no Pi OS Lite (para o DHT por bit-bang). A libusb vai em nativas/.
set -euo pipefail
cd "$(dirname "$0")"
AQUI=$PWD
USUARIO=$(id -un)

PY=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
ARQ=$(uname -m)
echo "== $ARQ, Python $PY"
[ "$ARQ/$PY" = aarch64/3.9 ] || { echo "ERRO: os wheels sao aarch64/cp39; este sistema e $ARQ/$PY"; exit 1; }

# Rede cabeada com IP fixo, se /boot/gaia-rede.conf existir. O arquivo fica no
# /boot (FAT) para poder ser escrito do notebook, sem root e sem regravar o SD:
#     ETH_IP=192.168.123.144/24
#     ETH_GW=192.168.123.99
#     ETH_DNS=192.168.123.99     # opcional; padrao = o gateway. Redes como a
#                                  # de laboratorio bloqueiam 1.1.1.1 e 8.8.8.8
# Vai para o /etc/dhcpcd.conf uma vez (marcador gaia-eth); mudou o arquivo, a
# secao e reescrita. O dhcpcd prefere a eth0 (metrica 202) ao wlan0 (303).
if [ -f /boot/gaia-rede.conf ] && [ -f /etc/dhcpcd.conf ]; then
  # shellcheck disable=SC1091
  . <(tr -d '\r' < /boot/gaia-rede.conf)
  BLOCO=$(printf '# gaia-eth inicio\ninterface eth0\nstatic ip_address=%s\nstatic routers=%s\nstatic domain_name_servers=%s\n# gaia-eth fim\n' \
    "$ETH_IP" "$ETH_GW" "${ETH_DNS:-$ETH_GW}")
  ATUAL=$(sed -n '/# gaia-eth inicio/,/# gaia-eth fim/p' /etc/dhcpcd.conf)
  if [ "$ATUAL" != "$BLOCO" ]; then
    sudo sed -i '/# gaia-eth inicio/,/# gaia-eth fim/d' /etc/dhcpcd.conf
    printf '\n%s\n' "$BLOCO" | sudo tee -a /etc/dhcpcd.conf >/dev/null
    echo "eth0 fixo: $ETH_IP via $ETH_GW"
    sudo systemctl restart dhcpcd || true
  fi
fi

# ---- dependencias: wheels -> lib/ ---------------------------------------------
ls wheels/*.whl >/dev/null 2>&1 || { echo "ERRO: sem wheels/ - rode imagem/montar_offline.sh no notebook"; exit 1; }
rm -rf lib.novo && mkdir lib.novo
for w in wheels/*.whl; do python3 -m zipfile -e "$w" lib.novo/; done
if [ -d nativas ]; then mkdir -p lib.novo/nativas && cp -a nativas/. lib.novo/nativas/; fi
rm -rf lib && mv lib.novo lib
LD_LIBRARY_PATH=$AQUI/lib/nativas PYTHONPATH=$AQUI/lib python3 - <<'PY'
import numpy, PIL, requests, pyrealsense2
print("numpy", numpy.__version__, "| Pillow", PIL.__version__, "| requests", requests.__version__, "| pyrealsense2 ok")
try:
    import adafruit_dht  # noqa: F401  (so funciona no Pi de verdade)
    print("adafruit_dht ok")
except Exception as erro:
    print("adafruit_dht indisponivel aqui (%s) - DHT fica sem leitura" % erro)
PY

# a librealsense fala com a camera por libusb, sem driver de kernel: precisa de permissao
sudo tee /etc/udev/rules.d/99-realsense-libusb.rules >/dev/null <<'R'
SUBSYSTEMS=="usb", ATTRS{idVendor}=="8086", ATTRS{idProduct}=="0b07", MODE="0666", GROUP="plugdev"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="8086", ATTRS{idProduct}=="0b3a", MODE="0666", GROUP="plugdev"
R
sudo udevadm control --reload-rules && sudo udevadm trigger || true
getent group gpio >/dev/null && sudo usermod -aG gpio,plugdev "$USUARIO" || true

sudo tee /etc/systemd/system/gaia-estacao.service >/dev/null <<S
[Unit]
Description=GAIA - estacao de captura (RealSense + sensores + interface)
After=network.target

[Service]
User=$USUARIO
WorkingDirectory=$AQUI
Environment=PYTHONPATH=$AQUI/lib
Environment=LD_LIBRARY_PATH=$AQUI/lib/nativas
ExecStart=/usr/bin/python3 $AQUI/estacao.py --config $AQUI/estacao.json
Restart=always
RestartSec=5
# 1 GB: se algo vazar, o systemd mata a estacao antes de o kernel matar o sshd
MemoryMax=600M

[Install]
WantedBy=multi-user.target
S
sudo systemctl daemon-reload
sudo systemctl enable gaia-estacao
sudo systemctl restart gaia-estacao
touch "$AQUI/.instalado"
IP=$(hostname -I | awk '{print $1}')
echo "== pronto: http://$IP:8080   (logs: journalctl -fu gaia-estacao)"
