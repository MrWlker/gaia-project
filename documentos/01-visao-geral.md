# 1. Visão geral e arquitetura

## O problema

Classificar uma foto de folha não basta. O objetivo do GAIA é acompanhar **cada
planta ao longo do tempo** (a mesma planta hoje, amanhã, na semana que vem) junto do
ambiente em que ela vive, para no futuro prever a trajetória de saúde dela. Isso
impõe três decisões de projeto que aparecem em todo o código.

### 1. Identidade da planta pela posição, não por rastreamento visual

A câmera é fixa, e a planta não anda. Então cada detecção é projetada do pixel para
uma coordenada **(x, y) em centímetros no canteiro**, e duas detecções a menos de
20 cm uma da outra, em dias diferentes, são a mesma planta (`servidor/app.py`,
`identidade_por_posicao`). Sem rede neural de rastreamento e sem cadastro manual.

A origem das coordenadas é o controlador do canteiro. Câmeras e sensores são
cadastrados pelo deslocamento até ele (`servidor/cadastrar.py`). A convenção de
eixos está em `gaia/geometria.py`.

### 2. O sensor que vale é o mais próximo da planta

Com vários sensores espalhados, a leitura atribuída a cada planta é a
**interpolação por distância** no ponto dela (`gaia/sensores.py`,
`interpolar_no_ponto`), não a média do canteiro. Cada leitura é uma linha por sensor
por instante (tabela `leitura`), porque uma coluna anônima "solo1" não sabe a que
pedaço do canteiro pertence.

### 3. Dado longitudinal não se refaz

Perder uma semana de captura custa uma semana de calendário. Por isso o nó grava
**primeiro em disco** (`fila/`) e envia depois. Se o servidor cai, a fila cresce e
escoa quando ele volta (`estacao/captura.py`, `enviar_fila`). Leitura que falha
vira `None`, nunca um valor inventado.

## Componentes

```
┌──────────────────────── nó de captura (estacao/) ────────────────────────┐
│ realsense.py   um pipeline, uma thread: último quadro para o vídeo ao vivo │
│                e quadro alinhado (cor + profundidade) para a captura       │
│ sensores.py    drivers plugáveis: cpu, realsense, dht11/22, ds18b20,       │
│                mcp3008, comando, simulado                                  │
│ estacao.py     laços de captura / sensores / envio + HTTP (interface, API) │
│ captura.py     fila em disco e envio ao servidor                           │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │ POST /ingest (imagem, profundidade, meta)
┌───────────────────────────────▼──────── servidor (servidor/) ─────────────┐
│ app.py         FastAPI + SQLAlchemy (SQLite por padrão)                     │
│ gaia/anotacao  máscara de vegetação -> caixas; Grad-CAM; imagem anotada    │
│ gaia/geometria pixel -> (x, y) do canteiro                                  │
│ gaia/sensores  leituras -> vetor do modelo; interpolação por distância      │
│ gaia/preditor  modelo GAIA (ConvNeXt multi-cabeça + ramo de sensores)       │
└────────────────────────────────────────────────────────────────────────────┘
```

## O que acontece numa captura

1. O nó pede à câmera o próximo quadro alinhado e grava três arquivos na fila:
   `<camera>_<instante>.jpg` (cor), `_prof.png` (profundidade em mm, 16 bits,
   alinhada à cor) e `.json` (leituras + intrínseca de fábrica da câmera).
2. Envia ao servidor (`POST /ingest`). Se falhar, fica na fila.
3. O servidor encontra as regiões de vegetação e, para cada uma: projeta no canteiro →
   acha ou cria a planta → interpola os sensores ali → classifica o recorte → calcula
   o Grad-CAM da condição.
4. Grava tudo e desenha a imagem anotada (caixas, máscara, foco).
5. A resposta volta ao nó e aparece na interface.

## Onde cada coisa roda

O Pi 3 tem 1 GB de RAM e um Cortex-A53 sem aceleração útil. Só o ConvNeXt pede
centenas de MB de ativação a 384 px. Por isso **nenhuma rede neural roda no Pi**: ele
captura, carimba, guarda e envia. A comparação entre variantes do modelo é de custo
de servidor, não de viabilidade embarcada.

O mesmo código do nó roda num PC (`estacao-notebook.json`), o que permite
desenvolver a interface e a inferência com a câmera no notebook antes de o Pi estar
pronto.
