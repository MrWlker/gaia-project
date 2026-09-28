#!/usr/bin/env bash
# Sobe o lado do notebook do GAIA: servidor de inferencia e (opcional) a
# interface da estacao simulada para ver a web antes de o Pi existir.
#
#     ./iniciar.sh                 # servidor em 0.0.0.0:8077 com o modelo de ../modelo
#     ./iniciar.sh --com-simulada  # + estacao simulada em http://localhost:8080
#
#     GAIA_MODELO=/outro/checkpoint.pt ./iniciar.sh
#
# O modelo vem do Hugging Face: python3 baixar_modelo.py (uma vez).
#
# Na primeira vez cadastra o canteiro, a D435 e o DHT11 (canais temp e
# umid_ar, que sao os que o no manda). Posicoes sao chute de bancada: meça com
# trena e corrija com cadastrar.py camera ... - sem isso a projecao erra.
set -euo pipefail
cd "$(dirname "$0")"
export GAIA_MODELO=${GAIA_MODELO:-../modelo}
ls "$GAIA_MODELO"/*.pt >/dev/null 2>&1 || [ -f "$GAIA_MODELO" ] || \
  echo "AVISO: sem modelo em $GAIA_MODELO - rode python3 baixar_modelo.py; subindo em ingestao pura"
export GAIA_BANCO=${GAIA_BANCO:-sqlite:///gaia.db}
export GAIA_MIDIA=${GAIA_MIDIA:-midia}
LOGS=${GAIA_LOGS:-logs}; mkdir -p "$LOGS"

if ! python3 cadastrar.py listar | grep -q '^camera'; then
  echo "== primeira vez: cadastrando canteiro, camera e DHT11"
  python3 cadastrar.py local canteiro-1 >/dev/null
  python3 cadastrar.py camera d435-frontal --local 1 --y -60 --z 80 --pitch -45 --fov 69.4 >/dev/null
  python3 cadastrar.py sensor dht11-ar --local 1 --canal temp --tipo temperature --central >/dev/null
  python3 cadastrar.py sensor dht11-ur --local 1 --canal umid_ar --tipo air_humidity --central >/dev/null
fi
if ! python3 cadastrar.py listar | grep -q 'd435-simulada'; then
  # local separado: imagem sintetica nao pode virar planta do canteiro real
  python3 cadastrar.py local bancada-simulada >/dev/null
  ID=$(python3 cadastrar.py listar | awk '$1=="local" && $3=="bancada-simulada"{print $2}')
  python3 cadastrar.py camera d435-simulada --local "$ID" --y -60 --z 80 --pitch -45 --fov 69.4 >/dev/null
fi
python3 cadastrar.py listar

if [ "${1:-}" = --com-simulada ]; then
  nohup python3 ../estacao/estacao.py --simular \
    --config ../estacao/estacao.json > "$LOGS/estacao-simulada.log" 2>&1 &
  echo "== estacao simulada: http://localhost:8080  (log em $LOGS/estacao-simulada.log)"
fi
echo "== servidor: http://0.0.0.0:8077  (docs em http://localhost:8077/docs), modelo $GAIA_MODELO"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port 8077
