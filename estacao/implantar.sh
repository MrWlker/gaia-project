#!/usr/bin/env bash
# Copia a estacao para o Pi (ou a VM) e instala.
#
#     ./implantar.sh vm                       # a VM do QEMU (localhost:2222)
#     ./implantar.sh pi@gaia-pi.local         # o Pi de verdade, no hotspot
#     ./implantar.sh pi@10.42.0.23
#
# Reimplantar preserva estacao.json, fila/, enviados/ e historico.db do lado
# de la: a configuracao dos sensores e a fila sao do no, nao do repositorio.
set -euo pipefail
cd "$(dirname "$0")"
ALVO=${1:?uso: ./implantar.sh vm | usuario@host}
SSH=(ssh)
if [ "$ALVO" = vm ]; then
  ALVO=pi@localhost
  SSH=(ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
fi
# tar pelo ssh, nao rsync: a imagem minima pode nao ter rsync, e sem rede nao instala
[ -e imagem/cache/nativas/libusb-1.0.so.0 ] || imagem/montar_offline.sh
"${SSH[@]}" "$ALVO" 'mkdir -p gaia/wheels'
"${SSH[@]}" "$ALVO" '[ -f gaia/estacao.json ]' || tar -c estacao.json | "${SSH[@]}" "$ALVO" 'tar -x -C gaia'
tar -c --exclude qemu --exclude imagem --exclude simulada --exclude fila --exclude enviados \
  --exclude historico.db --exclude __pycache__ --exclude .venv --exclude lib --exclude estacao.json . \
  | "${SSH[@]}" "$ALVO" 'tar -x -C gaia'
tar -c -C imagem/cache wheels nativas | "${SSH[@]}" "$ALVO" 'tar -x -C gaia'
"${SSH[@]}" "$ALVO" 'bash gaia/instalar_pi.sh'
