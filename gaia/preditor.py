"""
Inferencia do modelo GAIA numa imagem - o que o servidor chama por deteccao.

    python3 -m gaia.preditor --modelo modelo/ --imagem foto.jpg
    python3 -m gaia.preditor --modelo modelo/ --imagem foto.jpg \
        --temperatura 27.4 --umidade-ar 68 --umidade-solo 22

`--modelo` aceita a pasta baixada do Hugging Face (servidor/baixar_modelo.py)
ou o caminho direto do .pt. O checkpoint carrega tudo que a inferencia precisa:
arquitetura, tamanho de entrada, espaco de classes e se o ramo de sensores
existe - nada disso fica fixo no codigo.

Sensor ausente entra como ausente (flag 0), nunca como zero: o modelo foi
construido para distinguir "sem leitura" de "leitura 0".
"""
import argparse
import glob
import json
import os
from typing import Dict, List

import torch
import torch.nn.functional as F
from PIL import Image
from torchvision import transforms

from . import modelo as modelo_mod
from . import sensores as sensors_mod

IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp")


def transform_avaliacao(input_size: int):
    """O mesmo pre-processamento da avaliacao: Resize(1,14x) + CenterCrop. A
    rede NAO ve a borda da imagem - quem pinta explicacao sobre a foto precisa
    saber disso (ver explicacao.dividir_transform)."""
    return transforms.Compose([
        transforms.Resize(int(input_size * 1.14)),
        transforms.CenterCrop(input_size),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
    ])


class EspacoDeClasses:
    """Texto <-> indice por eixo (species, condition, hydration, agent, organ)."""

    def __init__(self, classes: Dict[str, List[str]]):
        self.classes = classes


def achar_checkpoint(caminho: str) -> str:
    if os.path.isfile(caminho):
        return caminho
    for nome in ("gaia.pt", "best.pt", "modelo.pt"):
        if os.path.isfile(os.path.join(caminho, nome)):
            return os.path.join(caminho, nome)
    achados = sorted(glob.glob(os.path.join(caminho, "*.pt")))
    if not achados:
        raise FileNotFoundError("nenhum .pt em %s - rode servidor/baixar_modelo.py" % caminho)
    return achados[0]


def carregar(caminho: str, device):
    # weights_only=False: o checkpoint carrega tambem o mapa de classes e a
    # configuracao, nao so tensores. So carregue checkpoints de fonte confiavel.
    arquivo = achar_checkpoint(caminho)
    ckpt = torch.load(arquivo, map_location=device, weights_only=False)
    # a tabela de faixas por especie vem publicada junto do modelo
    ref = os.path.join(os.path.dirname(os.path.abspath(arquivo)), "species_env.csv")
    if os.path.isfile(ref) and not os.environ.get("GAIA_REFERENCIA"):
        sensors_mod.usar_referencia(ref)
    use_sensors = ckpt.get("use_sensors")
    if use_sensors is None:
        use_sensors = any(k.startswith("fusion.encoder") for k in ckpt["state_dict"])
    kwargs = {"pretrained": False, "use_sensors": use_sensors,
              "input_size": ckpt.get("input_size")}
    if ckpt.get("sensor_axes") is not None:
        kwargs["sensor_axes"] = ckpt["sensor_axes"]
    model = modelo_mod.build_model(ckpt["model"], ckpt["head_sizes"], **kwargs)
    model.load_state_dict(ckpt["state_dict"])
    model.to(device).eval()
    ckpt["use_sensors"] = use_sensors
    return model, EspacoDeClasses(ckpt["classes"]), ckpt


class Predictor:
    """Carrega o checkpoint uma vez e classifica imagens. Reutilizavel por um
    servico HTTP: instancia no startup, chama predict() por requisicao."""

    def __init__(self, caminho_modelo, device=None):
        self.device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model, self.space, self.ckpt = carregar(caminho_modelo, self.device)
        self.transform = transform_avaliacao(self.ckpt["input_size"])
        self.use_sensors = self.ckpt.get("use_sensors", False)
        self.sensor_axes = tuple(self.ckpt.get("sensor_axes", ()))
        # eixos sem rotulo no treino: a cabeca existe, mas a saida e ruido
        self.trained_axes = set(self.ckpt.get("trained_axes", self.space.classes))

    def _forward(self, tensor, readings, species):
        sensor = torch.tensor([sensors_mod.encode(readings, species)],
                              dtype=torch.float32, device=self.device)
        return self.model(tensor, sensor if self.use_sensors else None)

    @staticmethod
    def _summarize(logits, space, topk, trained_axes):
        result = {}
        for axis, logit in logits.items():
            probs = F.softmax(logit[0], dim=0)
            names = space.classes[axis]
            k = min(topk, len(names))
            top_p, top_i = probs.topk(k)
            result[axis] = {
                "label": names[top_i[0].item()],
                "confidence": round(top_p[0].item(), 4),
                "trained": axis in trained_axes,
                "topk": [{"label": names[i.item()], "p": round(p.item(), 4)}
                         for p, i in zip(top_p, top_i)],
            }
        return result

    @torch.no_grad()
    def predict(self, image, topk=3, readings=None, species=None):
        """readings: {temperature, air_humidity, soil_moisture} em unidade
        fisica; chave ausente ou None = sensor sem leitura.

        species: a planta da foto, se voce souber. Serve so para achar a faixa
        de referencia e calcular o desvio ("6 C acima do que este tomate
        tolera"). Sem ela, duas passadas: a primeira estima a especie, a
        segunda usa a estimativa. E exato, porque a cabeca 'species' so ve a
        imagem e nao muda entre as passadas."""
        if isinstance(image, str):
            image = Image.open(image)
        tensor = self.transform(image.convert("RGB")).unsqueeze(0).to(self.device)

        logits = self._forward(tensor, readings, species)
        result = self._summarize(logits, self.space, topk, self.trained_axes)

        informed = any(v is not None for v in (readings or {}).values())
        if species is None and informed and self.use_sensors:
            guess = result.get("species", {}).get("label")
            if sensors_mod.reference_for(guess):
                logits = self._forward(tensor, readings, guess)
                result = self._summarize(logits, self.space, topk, self.trained_axes)
                result["_reference_species"] = guess
        return result


def main():
    ap = argparse.ArgumentParser(description="Classifica imagens com o modelo GAIA")
    ap.add_argument("--modelo", default="modelo", help="pasta ou .pt")
    ap.add_argument("--imagem", required=True, help="arquivo ou pasta")
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--temperatura", type=float)
    ap.add_argument("--umidade-ar", type=float)
    ap.add_argument("--umidade-solo", type=float)
    ap.add_argument("--json", help="grava os resultados neste arquivo")
    a = ap.parse_args()

    p = Predictor(a.modelo)
    leituras = {"temperature": a.temperatura, "air_humidity": a.umidade_ar,
                "soil_moisture": a.umidade_solo}
    arquivos = ([a.imagem] if os.path.isfile(a.imagem) else
                sorted(os.path.join(a.imagem, f) for f in os.listdir(a.imagem)
                       if f.lower().endswith(IMG_EXT)))
    saida = {}
    for f in arquivos:
        r = p.predict(f, topk=a.topk, readings=leituras)
        saida[f] = r
        print(os.path.basename(f))
        for eixo, v in r.items():
            if eixo.startswith("_"):
                continue
            aviso = "" if v["trained"] else "   (cabeca sem rotulo no treino - ignorar)"
            print("  %-10s %-28s %5.1f%%%s" % (eixo, v["label"], 100 * v["confidence"], aviso))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(saida, fh, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
