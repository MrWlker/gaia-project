# GAIA — monitoramento de plantas com câmera, sensores e IA

Protótipo embarcado de fenotipagem: um **nó de captura** (Raspberry Pi 3 + Intel
RealSense D435 + sensores) fotografa o canteiro em intervalos, e um **servidor de
inferência** (PC/notebook com GPU) encontra as plantas na imagem, reconhece cada uma
pela posição ao longo do tempo e classifica espécie, condição (saudável/doente),
agente, hidratação e órgão, usando também as leituras de temperatura e umidade.

```
 Nó de captura (Pi 3 / PC)                     Servidor de inferência (PC com GPU)
 ─────────────────────────                     ────────────────────────────────────
 D435 ─USB─► realsense.py ─┐                   servidor/app.py  :8077
 sensores.py ──────────────┼─► estacao.py ──►  /ingest ─► máscara de planta ─► caixas
                           │    :8080  fila          ─► posição no canteiro ─► id da planta
                           │    em disco             ─► sensores no ponto da planta
                           │                         ─► modelo GAIA (gaia/) ─► Grad-CAM
          interface web ◄──┘  ◄──────── resposta + imagem anotada ◄──┘
```

Este repositório tem **só código e documentação**. O modelo pré-treinado é
publicado à parte, no Hugging Face. O código de treino e os dados acompanham o
artigo e não estão aqui.

## Começo rápido (tudo num PC, com a câmera no USB)

```bash
pip install -r requirements.txt pyrealsense2
python3 servidor/baixar_modelo.py            # modelo -> modelo/
cd servidor && ./iniciar.sh                  # servidor em :8077
# outro terminal:
cd estacao && python3 estacao.py --config estacao-notebook.json
```

Abra **http://localhost:8080**. Sem câmera, use `--simular`, que gera cena e
sensores sintéticos.

## Organização

| pasta | o que é |
|---|---|
| `gaia/` | pacote de inferência: arquitetura, preditor, sensores, geometria, máscara, Grad-CAM, anotação |
| `servidor/` | API FastAPI + banco SQLite: recebe capturas, infere, guarda séries por planta |
| `estacao/` | nó de captura: RealSense, sensores plugáveis, fila offline, interface web |
| `estacao/imagem/` | gera o cartão SD do Raspberry Pi, com instalação 100% offline |
| `estacao/qemu/` | VM ARM64 que imita o Pi 3 para testar sem hardware |
| `ferramentas/` | exporta um checkpoint para publicar no Hugging Face |
| `documentos/` | como tudo funciona e como montar outros protótipos |
| `modelo/` | vazia; recebe o modelo baixado |

## Documentação

1. [Visão geral e arquitetura](documentos/01-visao-geral.md)
2. [Servidor e inferência](documentos/02-servidor-e-inferencia.md), incluindo a API
3. [Estação de captura e interface web](documentos/03-estacao.md)
4. [Raspberry Pi: imagem do SD, rede, ligação do DHT11](documentos/04-raspberry.md)
5. [Criando outros protótipos](documentos/05-novos-prototipos.md)
6. [O modelo: entradas, saídas, limites e publicação](documentos/06-modelo.md)
