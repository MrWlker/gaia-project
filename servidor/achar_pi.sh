#!/usr/bin/env bash
# Acompanha o Pi sem tela nem teclado, so pela rede do hotspot.
#
#     ./achar_pi.sh          # uma olhada
#     ./achar_pi.sh -f       # repete a cada 10 s ate a estacao responder
#
# As etapas, pelo que se ve de fora:
#   fora da rede      ainda no 1o boot (expande particao, firstrun) ou WiFi errado
#   so SSH (22)       firstrun feito, instalar_pi.sh rodando (apt + wheels)
#   interface (8080)  estacao no ar
REDE=${GAIA_REDE:-10.42.0}
olhar() {
  # varre a /24 do hotspot: ping em paralelo, um segundo cada
  for i in $(seq 2 254); do ping -c1 -W1 "$REDE.$i" >/dev/null 2>&1 & done; wait
  ips=$(ip neigh show | awk -v r="$REDE." 'index($1,r)==1 && $0 !~ /FAILED|INCOMPLETE/ {print $1}' | sort -u)
  ip_mdns=$(timeout 3 avahi-resolve -4 -n gaia-pi.local 2>/dev/null | awk '{print $2}')
  echo "[$(date +%H:%M:%S)] aparelhos no hotspot: ${ips:-nenhum}   gaia-pi.local: ${ip_mdns:-nao responde}"
  for ip in $ips; do
    # o celular tambem entra no hotspot: so interessa MAC de Raspberry
    mac=$(ip neigh show "$ip" | awk '{print $5}')
    case "$mac" in
      b8:27:eb:*|dc:a6:32:*|e4:5f:01:*|d8:3a:dd:*|28:cd:c1:*|2c:cf:67:*) ;;
      *) echo "  $ip  $mac - nao e Raspberry (celular/notebook?), ignorando"; continue;;
    esac
    ssh=$(timeout 2 bash -c "</dev/tcp/$ip/22" 2>/dev/null && echo sim || echo nao)
    web=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://$ip:8080/api/estado")
    if [ "$web" = 200 ]; then
      echo "  $ip  ESTACAO NO AR -> http://$ip:8080"
      curl -s -m 3 "http://$ip:8080/api/estado" | python3 -c "
import json,sys; e=json.load(sys.stdin); c=e['camera']; v=e['sensores'].get('valores',{})
print('     camera:', c['modo'], c.get('info',{}).get('usb') or '', c.get('erro') or '')
print('     sensores:', {k: v[k] for k in v})
print('     servidor:', e['servidor']['url'] or 'NAO ACHOU o notebook')"
      return 0
    elif [ "$ssh" = sim ]; then
      echo "  $ip  SSH aberto, interface ainda nao - instalando (ssh pi@$ip, senha gaia;"
      echo "       acompanhe: journalctl -fu gaia-instalar)"
    else
      echo "  $ip  responde ping, sem SSH ainda"
    fi
  done
  return 1
}
if [ "${1:-}" = -f ]; then
  until olhar; do sleep 10; done
else
  olhar
fi
