# 3. Estação de captura e interface web

A estação (`estacao/`) é o nó que fica junto das plantas. O mesmo código roda no
Raspberry Pi e num PC.

```bash
cd estacao
python3 estacao.py --config estacao-notebook.json   # PC com a D435 no USB
python3 estacao.py --config estacao.json            # Pi (quem chama é o systemd)
python3 estacao.py --simular                        # sem câmera nem sensores
```

Interface em `http://<ip>:8080`.

Ela usa só a biblioteca padrão do Python para o HTTP. Num Pi de 1 GB cada dependência
pesa: o FastAPI puxa o pydantic-core, que em armv7 compila em Rust por horas. As
dependências de verdade são numpy, Pillow, requests e pyrealsense2.

## A interface

- **Inferência** (painel principal): a última imagem, trocando sozinha a cada ciclo,
  com as abas *Anotada / Original / Profundidade*.
  - Caixas por planta, com número, espécie, condição e confiança. A cor diz a
    condição: verde saudável, âmbar incerto, vermelho doente.
  - Verde translúcido: a máscara de planta.
  - Vermelho: onde a rede olhou para decidir "doente".
  - Abaixo, cada planta com espécie, condição, agente, hidratação, órgão (com barras
    de confiança) e o ambiente estimado no ponto dela.
- **Câmera ao vivo**, secundária: MJPEG de cor, profundidade ou lado a lado,
  distância no centro da imagem, temperatura do ASIC da D435, botão *Inferir agora*
  e o intervalo de captura (5 s a 1 h, com efeito imediato).
- **Sensores**: um cartão por canal, com minigráfico da última hora e alerta de faixa.
- **Histórico**: 1 h, 6 h, 24 h ou 7 dias, com a faixa mín–máx de cada período e a
  média. CPU e ASIC começam desligados porque achatam as outras curvas.
- **Capturas**: galeria; passe o mouse para ver a profundidade, clique para os detalhes.
- **Sistema**: servidor em uso, modelos carregados, memória, disco e log.

A página é um único arquivo (`web/index.html`), sem CDN, porque o Pi pode estar sem
internet.

## Configuração (`estacao.json`)

```json
{
  "no": "canteiro-1",
  "camera": "d435-frontal",
  "servidor": ["http://192.168.123.99:8077", "http://10.42.0.1:8077", "http://127.0.0.1:8077"],
  "intervalo_s": 900,
  "amostragem_s": 15,
  "stream_fps": 8,
  "realsense": {"largura": 640, "altura": 480, "fps": 15},
  "sensores": [ ... ]
}
```

- `servidor` é uma **lista**: vale o primeiro que responder `/saude`. O mesmo arquivo
  serve no cabo, no hotspot e na VM.
- `camera` tem de existir no cadastro do servidor.
- `base` (opcional) aponta a pasta de dados; se for relativa, é relativa ao arquivo de
  configuração.
- `--simular` troca a câmera por uma cena sintética, troca cada sensor de hardware por
  um simulado nos **mesmos canais** e grava como câmera `d435-simulada`, numa pasta à
  parte.

## Sensores plugáveis (`sensores.py`)

Cada item de `sensores` vira um driver que devolve `{canal: valor}`:

| tipo | exemplo | observação |
|---|---|---|
| `cpu` | `{"tipo": "cpu"}` | temperatura do SoC |
| `realsense` | `{"tipo": "realsense"}` | ASIC e projetor da D435; acusa câmera cozinhando ao sol |
| `dht11` / `dht22` | `{"tipo": "dht11", "pino": 4, "canal_temp": "temp", "canal_umid": "umid_ar"}` | GPIO digital |
| `ds18b20` | `{"tipo": "ds18b20", "canal": "temp_solo", "id": "28-0123"}` | 1-Wire por sysfs |
| `mcp3008` | `{"tipo": "mcp3008", "canais": [0,1,2], "seco": {...}, "molhado": {...}}` | solo; o Pi não tem ADC |
| `comando` | `{"tipo": "comando", "canal": "x", "cmd": "python3 meu.py"}` | qualquer programa que imprima um número |
| `simulado` | `{"tipo": "simulado", "canal": "temp", "base": 25, "amplitude": 5}` | ciclo diário com ruído e falhas |

- Campos opcionais para a interface: `nome`, `unidade`, `min`, `max`. No DHT, a
  umidade usa `min_umid` e `max_umid`.
- Leitura que falha é `None`, nunca um valor inventado.
- O canal é o contrato com o servidor (veja o documento 02).

## RealSense D435

- Um pipeline só. Uma thread guarda o último quadro para o vídeo ao vivo; a captura
  pede o próximo quadro **alinhado** (a profundidade reprojetada na geometria da cor).
  O alinhamento custa caro no Pi e só roda na captura.
- Em USB 2, que é o caso do Pi 3, a estação força 640×480 a 15 fps, o modo
  cor + profundidade estável nessa conexão.
- A captura guarda a intrínseca de fábrica (fx, fy, cx, cy, distorção), o que abre
  caminho para projeção sem trena, e a profundidade por pixel em mm, a primeira
  medida métrica da planta (altura, volume de dossel).

## API do nó

| método | rota | o quê |
|---|---|---|
| GET | `/` | a interface |
| GET | `/stream/cor.mjpg`, `/stream/profundidade.mjpg` | vídeo ao vivo |
| GET | `/quadro/cor.jpg` | um quadro |
| GET | `/api/estado` | câmera, sensores, fila, servidor, sistema, log |
| GET | `/api/historico?horas=6` | séries dos sensores agregadas (~300 pontos) |
| GET | `/api/capturas?n=24` | últimas capturas com a resposta do servidor |
| GET | `/midia/{fila\|enviados}/{arquivo}[?cor=1]` | imagens; `?cor=1` colore a profundidade |
| POST | `/api/capturar` | captura e envia agora; devolve a inferência |
| POST | `/api/intervalo` | `{"intervalo_s": 10}` |

A interface não tem senha: ela vive na rede local do protótipo. Não exponha a porta
8080 à internet.
