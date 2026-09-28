#!/usr/bin/env bash
# Gera a imagem do cartao SD do Pi da horta, pronta para gravar e ligar.
#
#     ./gerar_imagem.sh                    # -> gaia-pi.img (~2,5 GB)
#     GAIA_SSID=x GAIA_SENHA=y ./gerar_imagem.sh   # rede da casa; o hotspot vira reserva
#
#     GAIA_ETH_IP=192.168.123.144/24 GAIA_ETH_GW=192.168.123.99 ./gerar_imagem.sh   # cabo, IP fixo
#
# A senha do WiFi vai por variavel de ambiente de proposito: nao fica no git.
#
# Depois:  gravar com dd (ver GRAVAR no fim) ou balenaEtcher. NAO use o
# Raspberry Pi Imager com "personalizacao do SO": ele sobrescreve o firstrun
# que este script instala.
#
# O QUE A IMAGEM FAZ SOZINHA
# --------------------------
#   1o boot   expande a particao e reinicia (do proprio Pi OS)
#   2o boot   firstrun.sh: hostname gaia-pi, usuario pi/gaia, SSH, WiFi do
#             hotspot, copia /boot/gaia -> /home/pi/gaia, reinicia
#   3o boot   gaia-instalar.service: assim que houver rede, roda
#             instalar_pi.sh (apt + wheels locais + systemd) e liga a estacao
#
# Tudo leva uns 10-15 min num Pi 3 B+. O log da instalacao vai para
# /boot/gaia-instalacao.log - se algo falhar, da para ler tirando o SD e
# pondo no notebook, sem monitor nem teclado.
#
# SEM ROOT
# --------
# So a particao de boot (FAT) e alterada, com mtools direto no arquivo .img.
# A ext4 do sistema nao e tocada aqui; quem escreve nela e o firstrun, no Pi.
#
# POR QUE BULLSEYE
# ----------------
# pyrealsense2 so tem wheel aarch64 para Python 3.8/3.9. O ultimo Pi OS com
# bullseye (Python 3.9) e o de 2025-05-06; os mais novos sao bookworm (3.11).
set -euo pipefail
cd "$(dirname "$0")"
RASP=$(cd .. && pwd)
CACHE=$PWD/cache
SAIDA=${GAIA_IMG:-$PWD/gaia-pi.img}
URL=https://downloads.raspberrypi.com/raspios_oldstable_lite_arm64/images/raspios_oldstable_lite_arm64-2025-05-07/2025-05-06-raspios-bullseye-arm64-lite.img.xz
SHA=21ad1b2ed62a5d6dd2251b42768133345875ebaca28cb1d367394fac4e126f8e
SSID=${GAIA_SSID:-GAIA-horta}
SENHA=${GAIA_SENHA:-gaia-horta-2026}
SENHA_PI=${GAIA_SENHA_PI:-gaia}
# o hotspot do notebook fica sempre como reserva, com prioridade menor: se a
# rede principal cair, o Pi ainda alcanca o notebook ligando ./hotspot.sh
RESERVA=""
if [ "$SSID" != GAIA-horta ]; then
  RESERVA=$(printf 'network={\n\tssid="GAIA-horta"\n\tpsk="gaia-horta-2026"\n\tpriority=5\n}')
fi
PAIS=${GAIA_PAIS:-BR}

for bin in mcopy mtype xz sfdisk openssl rsync; do
  command -v $bin >/dev/null || { echo "falta $bin (sudo apt install mtools xz-utils fdisk openssl rsync)"; exit 1; }
done
mkdir -p "$CACHE/wheels"

# ---- 1. imagem base, conferida ------------------------------------------------
if [ ! -f "$CACHE/bullseye.img.xz" ]; then
  curl -L --fail -o "$CACHE/bullseye.img.xz.part" "$URL"
  mv "$CACHE/bullseye.img.xz.part" "$CACHE/bullseye.img.xz"
fi
echo "$SHA  $CACHE/bullseye.img.xz" | sha256sum -c --quiet || { echo "SHA-256 nao confere"; exit 1; }

# ---- 2. wheels para o Pi (aarch64, Python 3.9): instalacao 100% offline ----
{ ls "$CACHE"/wheels/numpy-*cp39*aarch64*.whl && ls "$CACHE"/nativas/libusb-1.0.so.0; } >/dev/null 2>&1 || ./montar_offline.sh

# ---- 3. descompacta ----------------------------------------------------------
echo "== descompactando para $SAIDA"
xz -dc "$CACHE/bullseye.img.xz" > "$SAIDA"
INICIO=$(sfdisk -d "$SAIDA" | awk -F'[=,]' '/start=/{print $2+0; exit}')
FAT="$SAIDA@@$((INICIO * 512))"
export MTOOLS_SKIP_CHECK=1

# ---- 4. o que vai no /boot ---------------------------------------------------
ETAPA=$(mktemp -d)
trap 'rm -rf "$ETAPA"' EXIT

# codigo da estacao, sem o que e do notebook ou de teste
rsync -a --exclude imagem --exclude qemu --exclude simulada --exclude fila \
  --exclude enviados --exclude historico.db --exclude __pycache__ --exclude .venv \
  --exclude notebook-dados --exclude lib --exclude lib.novo --exclude .instalado \
  "$RASP/" "$ETAPA/gaia/"
cp -r "$CACHE/wheels" "$ETAPA/gaia/wheels"
cp -r "$CACHE/nativas" "$ETAPA/gaia/nativas"

# rede cabeada com IP fixo (opcional): quem aplica e o instalar_pi.sh no Pi
if [ -n "${GAIA_ETH_IP:-}" ]; then
  printf 'ETH_IP=%s\nETH_GW=%s\nETH_DNS="%s"\n' "$GAIA_ETH_IP" "${GAIA_ETH_GW:?GAIA_ETH_GW}" \
    "${GAIA_ETH_DNS:-$GAIA_ETH_GW}" > "$ETAPA/gaia-rede.conf"
  mcopy -o -i "$FAT" "$ETAPA/gaia-rede.conf" ::/
fi
: > "$ETAPA/ssh"
HASH=$(openssl passwd -6 "$SENHA_PI")

cat > "$ETAPA/firstrun.sh" <<FR
#!/bin/bash
# Gerado por gerar_imagem.sh. Roda uma vez, como root, e se apaga.
set +e
exec > /boot/gaia-firstrun.log 2>&1

ATUAL=\$(tr -d " \t\n\r" < /etc/hostname)
echo gaia-pi > /etc/hostname
sed -i "s/127.0.1.1.*\$ATUAL/127.0.1.1\tgaia-pi/g" /etc/hosts

# usuario pi (o Pi OS novo nao vem com usuario padrao)
if [ -f /usr/lib/userconf-pi/userconf ]; then
  /usr/lib/userconf-pi/userconf 'pi' '$HASH'
else
  PRIMEIRO=\$(getent passwd 1000 | cut -d: -f1)
  echo "\$PRIMEIRO:"'$HASH' | chpasswd -e
  [ "\$PRIMEIRO" != pi ] && usermod -l pi "\$PRIMEIRO" && usermod -m -d /home/pi pi && groupmod -n pi "\$PRIMEIRO"
fi
usermod -aG plugdev,video,gpio,i2c,spi,dialout pi

systemctl enable ssh

# WiFi do hotspot do notebook. country e obrigatorio: sem ele o bullseye
# deixa o radio bloqueado pelo rfkill.
cat > /etc/wpa_supplicant/wpa_supplicant.conf <<'WPA'
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=$PAIS

network={
	ssid="$SSID"
	psk="$SENHA"
	priority=20
}
$RESERVA
WPA
chmod 600 /etc/wpa_supplicant/wpa_supplicant.conf
rfkill unblock wifi
for f in /var/lib/systemd/rfkill/*:wlan; do echo 0 > "\$f"; done

rm -f /etc/localtime
echo "America/Sao_Paulo" > /etc/timezone
dpkg-reconfigure -f noninteractive tzdata

# a estacao
cp -r /boot/gaia /home/pi/gaia
chown -R pi:pi /home/pi/gaia

cat > /etc/systemd/system/gaia-instalar.service <<'SVC'
[Unit]
Description=GAIA - instalacao da estacao no primeiro boot com rede
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/home/pi/gaia/.instalado

[Service]
Type=oneshot
User=pi
WorkingDirectory=/home/pi/gaia
ExecStart=/bin/bash -o pipefail -c 'bash /home/pi/gaia/instalar_pi.sh 2>&1 | sudo tee /boot/gaia-instalacao.log'
ExecStartPost=/usr/bin/touch /home/pi/gaia/.instalado
TimeoutStartSec=1800
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
SVC
systemctl enable gaia-instalar.service

rm -f /boot/firstrun.sh
sed -i 's| systemd.run.*||g' /boot/cmdline.txt
exit 0
FR

# ---- 5. grava no FAT ---------------------------------------------------------
mcopy -o -i "$FAT" "$ETAPA/ssh" "$ETAPA/firstrun.sh" ::/
mcopy -o -s -i "$FAT" "$ETAPA/gaia" ::/
CMD=$(mtype -i "$FAT" ::/cmdline.txt | tr -d '\r\n')
case "$CMD" in *systemd.run=*) ;; *)
  CMD="$CMD systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target";;
esac
printf '%s\n' "$CMD" > "$ETAPA/cmdline.txt"
mcopy -o -i "$FAT" "$ETAPA/cmdline.txt" ::/cmdline.txt

echo "== cmdline.txt: $(mtype -i "$FAT" ::/cmdline.txt)"
mdir -i "$FAT" ::/gaia | tail -3
ls -lh "$SAIDA"
cat <<FIM

PRONTO: $SAIDA
  usuario pi / senha $SENHA_PI   hostname gaia-pi   WiFi "$SSID" / "$SENHA"

GRAVAR (confira o dispositivo com lsblk - dd no disco errado apaga o disco):
  lsblk -o NAME,SIZE,MODEL,TRAN
  sudo dd if=$SAIDA of=/dev/sdX bs=4M conv=fsync status=progress

LIGAR: notebook e Pi na mesma rede ("$SSID"), servidor/iniciar.sh no notebook.
       O Pi acha o notebook por mDNS; interface em http://gaia-pi.local:8080
FIM
