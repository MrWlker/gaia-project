# 2. Servidor e inferência

## Subir

```bash
pip install -r requirements.txt        # torch com CUDA: instale a versão da sua GPU
python3 servidor/baixar_modelo.py      # uma vez; modelo -> modelo/
cd servidor
./iniciar.sh                           # 0.0.0.0:8077, modelo de ../modelo
./iniciar.sh --com-simulada            # + uma estação simulada em :8080
```

Na primeira vez, o `iniciar.sh` cadastra um canteiro de exemplo (`canteiro-1`, câmera
`d435-frontal`, DHT11 nos canais `temp` e `umid_ar`) e uma bancada separada para a
simulação. **As posições cadastradas são de exemplo**: meça com trena e corrija
(veja abaixo), senão a projeção no canteiro erra.

| variável | padrão | para quê |
|---|---|---|
| `GAIA_MODELO` | `../modelo` | pasta ou `.pt` do modelo; vazio = só ingestão, sem classificar |
| `GAIA_BANCO` | `sqlite:///gaia.db` | qualquer URL do SQLAlchemy |
| `GAIA_MIDIA` | `midia` | onde ficam imagens, profundidade e anotações |
| `GAIA_REFERENCIA` | (a da pasta do modelo) | tabela de faixas ambientais por espécie |

Sem modelo, o servidor funciona em **ingestão pura**: localiza plantas, guarda a série e
os sensores. Serve para começar a coletar antes de o modelo final existir.

## Cadastro: canteiro, câmeras e sensores

```bash
python3 cadastrar.py local canteiro-1
python3 cadastrar.py camera d435-frontal --local 1 --x 0 --y -60 --z 80 --pitch -45 --fov 69.4
python3 cadastrar.py sensor ar --local 1 --canal temp --tipo temperature --central
python3 cadastrar.py sensor solo-a --local 1 --canal soil1 --tipo soil_moisture --x 20 --y 30
python3 cadastrar.py listar
```

- Distâncias em cm a partir do controlador; ângulos em graus. `pitch` negativo olha
  para baixo. A D435 a 640×480 tem FOV horizontal de cor de ~69°; a estação mostra o
  valor exato, lido da câmera.
- `--canal` é o nome que o nó envia (`temp`, `umid_ar`, `soil1`…). **Canal não
  cadastrado é ignorado** pelo servidor, por isso a temperatura da CPU pode ir junto
  sem poluir o dado da planta.
- `--tipo` diz o que a leitura significa para o modelo. Só `temperature`,
  `air_humidity` e `soil_moisture` entram no vetor de sensores. Outros tipos ficam
  gravados, mas o modelo não os usa.
- Separe **bancada e canteiro real em locais diferentes**. Dado de teste misturado à
  série de uma planta real não se separa depois.

## O pipeline do `/ingest`

1. **Plantas na imagem** (`gaia/anotacao.py`, `caixas_pela_mascara`)
   - Máscara de vegetação pelo índice de excesso de verde (ExG) com limiar de Otsu,
     mais um **piso absoluto de 0,10**. Sem o piso, uma cena sem planta (teto,
     parede) ainda teria "metade verde". Medido com a D435: teto p99 = 0,085, folha
     acima de 0,5.
   - As folhas próximas são agrupadas em uma planta. Cada grupo com mais de 1,5 % do
     quadro vira uma caixa (no máximo 6).
   - **Sem vegetação, não há caixa nem classificação**: a resposta diz "nenhuma
     planta". Classificar um quadro escuro produziria uma planta falsa na série.
   - Não é um detector treinado. Duas plantas encostadas viram uma caixa só.
2. **Posição**: a base da caixa é projetada no plano do solo (`gaia/geometria.py`).
   Se o raio não cruza o solo (pitch errado, caixa no céu), a detecção é guardada sem
   posição, em vez de inventar uma.
3. **Identidade**: planta mais próxima a menos de 20 cm, ou uma nova. A posição é
   refinada por média a cada observação.
4. **Sensores no ponto** (`gaia/sensores.py`): interpolação por inverso da
   distância; o sensor `--central` entra como fallback.
5. **Classificação** do recorte (`gaia/preditor.py`), com o vetor de sensores
   daquela planta.
6. **Grad-CAM** da cabeça `condition` no recorte, recortado pela máscara de planta e
   com opacidade proporcional a P(doente). Mostra **onde a rede olhou**, não é uma
   segmentação de lesão: o modelo nunca viu máscara de lesão no treino.
7. **Imagem anotada** `<nome>_anotada.jpg`, servida em `/midia/...`.

## API

| método | rota | o quê |
|---|---|---|
| POST | `/ingest` | multipart: `imagem` (jpg), `meta` (JSON), `profundidade` (png, opcional) |
| GET | `/saude` | `{ok, classificador, detector, banco}` |
| GET | `/plantas` | plantas conhecidas, posição e número de observações |
| GET | `/planta/{id}/serie` | série temporal de uma planta: condição e sensores por instante |
| GET | `/midia/{caminho}` | imagens gravadas e anotadas |
| GET | `/docs` | documentação interativa (FastAPI) |

`meta` do `/ingest`:

```json
{
  "no": "canteiro-1",
  "camera": "d435-frontal",
  "instante": "2026-09-28T22:44:06+00:00",
  "imagem": "d435-frontal_20260928T224406Z.jpg",
  "leituras": {"temp": 24.1, "umid_ar": 66.0, "soil1": null},
  "camera_info": {"intrinseca_cor": {"fx": 615.3, "fy": 615.1, "cx": 322.5, "cy": 240.7}}
}
```

A câmera precisa estar cadastrada (senão, 404: sem extrínseca não há como projetar).
Leitura `null` é "sem leitura".

Resposta:

```json
{
  "observacao": 12,
  "anotada": "d435-frontal/2026-09-28/d435-frontal_20260928T224406Z_anotada.jpg",
  "area_planta": 0.074,
  "fonte_caixas": "mascara_exg",
  "deteccoes": [{
    "planta": 3, "x": 12.4, "y": 31.0,
    "especie": "lactuca_sativa", "p_especie": 0.41,
    "condicao": "healthy", "p_condicao": 0.95, "p_doente": 0.05,
    "agente": "none", "p_agente": 0.9,
    "hydration": {"label": "hydrated", "p": 0.6}, "organ": {"label": "leaf", "p": 0.8},
    "temperatura": 22.5, "umidade_ar": 73.2, "umidade_solo": 40.7,
    "observacoes": 14,
    "caixa": {"xmin": 326, "ymin": 340, "xmax": 450, "ymax": 416}
  }]
}
```

## Usar só a inferência, sem servidor

```python
from gaia.preditor import Predictor
p = Predictor("modelo/")
r = p.predict("foto.jpg", readings={"temperature": 27.4, "air_humidity": 68})
print(r["condition"]["label"], r["condition"]["confidence"])
```

Ou pela linha de comando: `python3 -m gaia.preditor --modelo modelo/ --imagem pasta/`.

## Custo

Numa RTX 3050 (4 GB), o ConvNeXt a 384 px carrega em ~4 s e classifica em ~0,35 s por
recorte. O Grad-CAM custa uma passada para trás por recorte. Com até 6 plantas por
quadro, um ciclo de 10 s cabe com folga. A 10 s por ciclo, as imagens somam uns
700 MB por dia em `midia/`.
