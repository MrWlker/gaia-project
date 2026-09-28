#!/usr/bin/env bash
# Hotspot do notebook para o Pi da horta, e o servidor escutando nele.
#
#     ./hotspot.sh ligar       # cria (na primeira vez) e sobe a rede GAIA-horta
#     ./hotspot.sh servidor    # uvicorn em 0.0.0.0:8077, alcancavel pelo Pi
#     ./hotspot.sh status      # IP do hotspot e quem esta conectado
#     ./hotspot.sh desligar    # volta a placa para o WiFi de sempre
#
# A PLACA DESTE NOTEBOOK NAO FAZ AP E CLIENTE AO MESMO TEMPO
# ---------------------------------------------------------
# `iw list` diz "interface combinations are not supported": subir o hotspot
# desconecta o WiFi de onde ele estiver. A internet do notebook continua pelo cabo
# (eno1), e o NetworkManager faz NAT dela para o Pi.
#
# 2,4 GHz, NAO 5
# --------------
# O WiFi do Pi 3 (BCM43438) e so 2,4 GHz. Hotspot em 5 GHz e invisivel para ele.
#
# O IP
# ----
# `ipv4.method shared` da ao notebook 10.42.0.1 e distribui 10.42.0.x por DHCP.
# E por isso que 10.42.0.1:8077 e o primeiro da lista `servidor` do estacao.json.
set -euo pipefail
IF=${GAIA_WIFI_IF:-wlp4s0}
CON=gaia-hotspot
SSID=${GAIA_SSID:-GAIA-horta}
SENHA=${GAIA_SENHA:-gaia-horta-2026}
PORTA=${GAIA_PORTA:-8077}

case "${1:-status}" in
  ligar)
    if ! nmcli -t -f NAME con show | grep -qx "$CON"; then
      nmcli con add type wifi ifname "$IF" con-name "$CON" autoconnect no ssid "$SSID" \
        802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
        ipv4.method shared ipv6.method ignore \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$SENHA"
    fi
    nmcli con up "$CON"
    echo "rede $SSID (senha $SENHA) no ar; notebook = $(nmcli -g IP4.ADDRESS dev show "$IF" | cut -d/ -f1)"
    ;;
  desligar)
    nmcli con down "$CON" || true
    ;;
  servidor)
    cd "$(dirname "$0")"
    # 0.0.0.0: o padrao do uvicorn e 127.0.0.1, e o Pi nao alcancaria
    exec python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORTA"
    ;;
  status)
    nmcli -f GENERAL.CONNECTION,IP4.ADDRESS dev show "$IF" || true
    echo "-- clientes (DHCP do NetworkManager):"
    ip neigh show dev "$IF" 2>/dev/null | grep -v FAILED || echo "(nenhum)"
    ;;
  *) echo "uso: $0 ligar|servidor|status|desligar"; exit 1;;
esac
