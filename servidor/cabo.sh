#!/usr/bin/env bash
# Pi no cabo direto da eno1, com o notebook como gateway 192.168.123.99.
#
#     ./cabo.sh ligar      # hotspot off, WiFi como cliente, eno1 compartilhada
#     ./cabo.sh status
#     ./cabo.sh desligar   # eno1 volta ao DHCP de sempre (conexao AUTO)
#
# `ipv4.method shared` com endereco proprio: o NetworkManager poe a eno1 em
# 192.168.123.99/24, liga o ip_forward e faz NAT para a rota padrao - que,
# com o cabo tomado pelo Pi, e o WiFi. O Pi usa IP fixo (192.168.123.144,
# /boot/gaia-rede.conf); o DHCP que o modo shared tambem sobe nao atrapalha.
#
# A placa WiFi deste notebook nao faz AP e cliente ao mesmo tempo: o hotspot
# do PC tem de cair para o WiFi virar a saida de internet.
set -euo pipefail
ETH=${GAIA_ETH_IF:-eno1}
CON=gaia-cabo
WIFI=${GAIA_WIFI_REDE:?defina GAIA_WIFI_REDE=<conexao WiFi que da internet ao PC>}
IP=${GAIA_GW:-192.168.123.99/24}

case "${1:-status}" in
  ligar)
    if ! nmcli -t -f NAME con show | grep -qx "$CON"; then
      nmcli con add type ethernet ifname "$ETH" con-name "$CON" autoconnect no \
        ipv4.method shared ipv4.addresses "$IP" ipv6.method ignore
    fi
    nmcli con down Hotspot 2>/dev/null || true
    nmcli con up "$WIFI" || echo "AVISO: $WIFI nao conectou - o Pi fica sem internet (GAIA_WIFI_REDE=outra)"
    nmcli con up "$CON"
    echo "== $ETH = $IP compartilhando a internet do WiFi ($WIFI)"
    echo "   Pi esperado em 192.168.123.144:  ping 192.168.123.144 ; http://192.168.123.144:8080"
    ;;
  desligar)
    nmcli con down "$CON" || true
    nmcli con up AUTO || true
    ;;
  status)
    nmcli -t -f NAME,DEVICE con show --active
    ip -br addr show "$ETH"
    ping -c1 -W2 192.168.123.144 >/dev/null 2>&1 && echo "Pi responde em 192.168.123.144" || echo "Pi nao responde em 192.168.123.144"
    ;;
  *) echo "uso: $0 ligar|status|desligar"; exit 1;;
esac
