#!/usr/bin/env python3
"""
Prepara um checkpoint para publicar no Hugging Face: so o que a inferencia usa.

    python3 ferramentas/exportar_modelo.py caminho/best.pt publicar/ [species_env.csv]
    huggingface-cli upload usuario/gaia-convnext publicar/ .

Mantem: arquitetura, head_sizes, input_size, classes, ramo de sensores e os
pesos. Tira o que so interessa ao treino (passo global, epoca, otimizador,
metricas de validacao) - o checkpoint publicado nao deve depender do codigo
de treino, e nem revelar mais do que o artigo publica.

Grava tambem classes.json (legivel sem torch) e um README.md de cartao de
modelo para completar a mao.
"""
import json
import os
import sys

import torch

MANTER = ("model", "name", "head_sizes", "input_size", "use_sensors", "sensor_fields",
          "sensor_axes", "trained_axes", "classes", "state_dict")


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    origem, destino = sys.argv[1], sys.argv[2]
    if len(sys.argv) == 4:
        # faixas ambientais por especie: o preditor as acha ao lado do .pt
        import shutil
        shutil.copy(sys.argv[3], os.path.join(destino if os.path.isdir(destino) else
                                              (os.makedirs(destino) or destino), "species_env.csv"))
    os.makedirs(destino, exist_ok=True)
    ckpt = torch.load(origem, map_location="cpu", weights_only=False)
    faltam = [k for k in ("model", "head_sizes", "classes", "state_dict") if k not in ckpt]
    if faltam:
        sys.exit("checkpoint sem %s" % faltam)
    limpo = {k: ckpt[k] for k in MANTER if k in ckpt}
    torch.save(limpo, os.path.join(destino, "gaia.pt"))
    with open(os.path.join(destino, "classes.json"), "w") as fh:
        json.dump(limpo["classes"], fh, indent=1, ensure_ascii=False)
    cartao = os.path.join(destino, "README.md")
    if not os.path.exists(cartao):
        with open(cartao, "w") as fh:
            fh.write(CARTAO.format(
                arq=limpo["model"], entrada=limpo.get("input_size"),
                cabecas=", ".join("%s (%d)" % kv for kv in limpo["head_sizes"].items()),
                sensores=", ".join(limpo.get("sensor_fields", [])) or "nenhum",
                treinados=", ".join(sorted(limpo.get("trained_axes", []))) or "?"))
    tam = os.path.getsize(os.path.join(destino, "gaia.pt")) / 1e6
    print("gaia.pt (%.0f MB), classes.json e README.md em %s" % (tam, destino))
    print("descartado: %s" % sorted(set(ckpt) - set(limpo)))


CARTAO = """---
license: other
library_name: pytorch
tags: [plant-disease, agriculture, image-classification, multi-task]
---

# GAIA - {arq}

Modelo multi-cabeca de saude de plantas: imagem + (opcional) leituras de
temperatura, umidade do ar e umidade do solo.

- arquitetura: {arq} (torchvision), entrada {entrada} px
- cabecas: {cabecas}
- cabecas com rotulo no treino: {treinados}
- sensores: {sensores} (ausente = flag 0, nunca zero)

Uso: https://github.com/MrWlker/gaia-project

```python
from gaia.preditor import Predictor
p = Predictor("gaia.pt")
p.predict("foto.jpg", readings={{"temperature": 26.0, "air_humidity": 70}})
```

## Limitacoes

COMPLETAR: dados de treino, metricas por dominio (laboratorio x campo), e o
artigo de referencia.
"""


if __name__ == "__main__":
    main()
