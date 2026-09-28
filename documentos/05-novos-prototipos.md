# 5. Criando outros protótipos

O sistema foi dividido para que cada parte possa ser trocada sem mexer nas outras.
O **contrato** entre elas é pequeno: o nó manda `POST /ingest` com uma imagem e um
JSON de leituras por canal; o servidor sabe onde cada câmera e cada sensor estão.

## Um sensor novo no mesmo nó

Sem código: se o sensor tem um programa que imprime um número, use o tipo `comando`:

```json
{"tipo": "comando", "canal": "luz", "nome": "Luminosidade", "unidade": "lx", "cmd": "python3 bh1750.py"}
```

Com código: crie uma classe em `estacao/sensores.py` e registre em `TIPOS`:

```python
class BH1750(Driver):
    def descrever(self):
        return [(self.cfg.get("canal", "luz"), "Luminosidade", "lx")]

    def ler(self):
        try:
            return {self.descrever()[0][0]: ler_bh1750(self.cfg.get("endereco", 0x23))}
        except Exception:
            return {self.descrever()[0][0]: None}   # falha = None, nunca valor inventado

TIPOS["bh1750"] = BH1750
```

Para a leitura entrar na série da planta, cadastre o canal no servidor com posição
(`cadastrar.py sensor ... --canal luz`). Para o **modelo** usá-la, ela precisa ser um
dos tipos que ele conhece (`temperature`, `air_humidity`, `soil_moisture`). Um tipo
novo exige retreinar o ramo de sensores.

## Um nó em outro hardware (ESP32-CAM, celular, outro SBC)

Qualquer coisa que faça um POST multipart é um nó. Exemplo mínimo em Python:

```python
import json, requests, datetime
meta = {"no": "esp-1", "camera": "esp32-cam-1",
        "instante": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "imagem": "foto.jpg", "leituras": {"temp": 24.0, "umid_ar": None}}
r = requests.post("http://SERVIDOR:8077/ingest",
                  files={"imagem": ("foto.jpg", open("foto.jpg", "rb"), "image/jpeg")},
                  data={"meta": json.dumps(meta)})
print(r.json())
```

Regras que valem para qualquer nó:

1. **Cadastre a câmera** no servidor, com posição, yaw, pitch, FOV e resolução.
   Sem isso ela recebe 404.
2. **Grave antes de enviar.** Rede cai, e dado longitudinal não se refaz.
   `estacao/captura.py` (`enviar_fila`) é a referência.
3. **Relógio certo.** O `instante` vem do nó. Um Pi sem internet acorda com a data da
   última vez que desligou (não tem RTC): acerte o relógio antes de coletar, por NTP
   na rede local ou com um módulo RTC.
4. **Sem leitura é `null`.** Zero é leitura.
5. Câmera com pouca resolução: o modelo foi treinado com aumento de dados contra
   borrão, baixa resolução e ruído, mas desfoque ainda é o defeito que mais custa.

## Mais de uma câmera

Cadastre cada uma com a própria posição. Como a identidade da planta é pela
coordenada no canteiro, a mesma planta vista por duas câmeras cai no mesmo id, sem
nenhum código a mais.

## Outra forma de achar plantas

`gaia/anotacao.py` (`caixas_pela_mascara`) devolve `(mascara, caixas)`. Para trocar
por um detector treinado, implemente a mesma assinatura e chame-o em `detectar()`
(`servidor/app.py`). Cada caixa é `{"xmin", "ymin", "xmax", "ymax", "score", "fonte"}`,
em pixels da imagem original.

## Outro modelo

O preditor lê do checkpoint a arquitetura, o tamanho de entrada, as classes e se há
ramo de sensores. Qualquer checkpoint no mesmo formato funciona sem mudar código:
`GAIA_MODELO=/caminho/outro.pt ./iniciar.sh`. As arquiteturas suportadas estão em
`gaia/modelo.py` (MobileNetV3, EfficientNet B0/V2-S, ConvNeXt-Base). Uma MobileNet
roda em CPU e serve para servidor sem GPU.

## Usar só o pacote `gaia`

```python
from gaia.preditor import Predictor
from gaia import anotacao
import numpy as np
from PIL import Image

p = Predictor("modelo/")
rgb = np.asarray(Image.open("canteiro.jpg").convert("RGB"))
mascara, caixas = anotacao.caixas_pela_mascara(rgb)
for c in caixas:
    recorte = Image.fromarray(rgb).crop((int(c["xmin"]), int(c["ymin"]), int(c["xmax"]), int(c["ymax"])))
    print(c, p.predict(recorte)["condition"])
```
