"""
Anotacao de uma observacao: caixas, mascara de planta e foco da condicao.

TRES CAMADAS, TRES ORIGENS - e a interface diz qual e qual
-----------------------------------------------------------
  caixas    regioes conexas da MASCARA DE PLANTA, nao um detector treinado.
            Este repositorio nao traz detector treinado; ate existir
            um, a caixa e "onde ha vegetacao contigua". Duas plantas que se
            tocam viram uma caixa so - limitacao conhecida, e visivel.
  verde     a mascara: excesso de verde (ExG) + Otsu + fechamento, de
            gaia/explicacao.py. Processamento classico, sem modelo.
  vermelho  Grad-CAM da cabeca 'condition' dentro de cada caixa, com opacidade
            escalada por P(diseased). E onde a rede OLHOU para decidir, nao
            uma segmentacao de lesao medida - o modelo nunca viu mascara de
            lesao no treino.
"""
import os

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont

from . import explicacao as ex
from . import sensores as sensores_mod

AREA_MIN = 0.015        # fracao da imagem; menos que isso e folha solta/ruido
MAX_CAIXAS = 6          # cada caixa custa uma classificacao + um Grad-CAM
PISO_EXG = 0.10         # ver caixas_pela_mascara
GRUPO_PX = 6            # folhas a menos de ~2x isso (na escala de 320 px) = mesma planta
LADO_MASCARA = 320      # a mascara roda reduzida: e contorno, nao detalhe

_FONTE = None


def fonte(tamanho=15):
    global _FONTE
    if _FONTE is None:
        for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
            if os.path.exists(f):
                _FONTE = ImageFont.truetype(f, tamanho)
                break
        else:
            _FONTE = ImageFont.load_default()
    return _FONTE


def caixas_pela_mascara(rgb):
    """rgb (H,W,3) -> (mascara bool HxW, [caixas]). Sem vegetacao, nenhuma
    caixa: quem chama decide se cai para o quadro inteiro."""
    from scipy import ndimage
    h, w = rgb.shape[:2]
    escala = LADO_MASCARA / float(max(h, w))
    pequena = np.asarray(Image.fromarray(rgb).resize(
        (max(1, int(w * escala)), max(1, int(h * escala))), Image.BILINEAR))
    # O Otsu separa "mais verde" de "menos verde" DENTRO da imagem: numa cena
    # sem planta (teto cinza, parede) ele ainda acha uma metade "verde". O piso
    # absoluto de ExG corta isso. Medido com a D435 (28/09): teto branco, com o
    # balanco de branco esverdeado da camera, p90 = 0,068 e p99 = 0,085; folha
    # passa de 0,5. Em 0,10 o teto cai para 0,2% do quadro e a folha fica.
    x = pequena.astype(np.float32)
    soma = x.sum(axis=2) + 1e-6
    exg = (2 * x[..., 1] - x[..., 0] - x[..., 2]) / soma
    m = ex.mascara_planta(pequena) & (exg > PISO_EXG)
    if m.any():
        from scipy import ndimage as _nd
        m = _nd.binary_opening(m, iterations=2)          # tira pontinhos soltos
    mascara = np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize(
        (w, h), Image.NEAREST)) > 0
    # Uma planta sao varias folhas que o ExG separa (caule fino, sombra entre
    # elas). Agrupa por proximidade dilatando SO para rotular; a area e a caixa
    # saem dos pixels de verdade de cada grupo, entao a caixa continua justa.
    grupos, n = ndimage.label(ndimage.binary_dilation(m, iterations=GRUPO_PX))
    caixas = []
    for i in range(1, n + 1):
        ys, xs = np.nonzero(m & (grupos == i))
        area = float(ys.size) / m.size
        if area < AREA_MIN:
            continue
        y0, y1 = ys.min() / escala, (ys.max() + 1) / escala
        x0, x1 = xs.min() / escala, (xs.max() + 1) / escala
        caixas.append({"xmin": x0, "ymin": y0, "xmax": min(x1, w), "ymax": min(y1, h),
                       "score": round(area, 4), "fonte": "mascara_exg"})
    caixas.sort(key=lambda c: -c["score"])
    return mascara, caixas[:MAX_CAIXAS]


class Focador:
    """Grad-CAM da cabeca 'condition', uma instancia por modelo carregado."""

    def __init__(self, predictor):
        self.p = predictor
        self.cam = ex.GradCAM(predictor.model, ex.camada_alvo(predictor.model))
        self.geometria, self.tensorizar = ex.dividir_transform(predictor.transform)
        nomes = predictor.space.classes.get("condition", [])
        self.alvo = nomes.index("diseased") if "diseased" in nomes else 0

    def calor(self, recorte, leituras, especie):
        """Mapa 0..1 no tamanho do RECORTE. O que ficou fora do CenterCrop da
        avaliacao fica 0: a rede nao viu, e pintar ali seria inventar."""
        vista = self.geometria(recorte)
        s = vista.size[0]
        tensor = self.tensorizar(vista).unsqueeze(0).to(self.p.device)
        sensor = torch.tensor([sensores_mod.encode(leituras, especie)],
                              dtype=torch.float32, device=self.p.device)
        with torch.enable_grad():
            mapa = self.cam.mapa(tensor, sensor if self.p.use_sensors else None,
                                 "condition", self.alvo, (s, s))
        w, h = recorte.size
        # desfaz Resize(lado menor -> r) + CenterCrop(s)
        r = s * 1.14 if min(w, h) else s
        fator = r / float(min(w, h))
        W, H = int(round(w * fator)), int(round(h * fator))
        tela = np.zeros((H, W), np.float32)
        x0, y0 = (W - s) // 2, (H - s) // 2
        tela[max(0, y0):y0 + s, max(0, x0):x0 + s] = \
            mapa[max(0, -y0):max(0, -y0) + min(s, H), max(0, -x0):max(0, -x0) + min(s, W)]
        return np.asarray(Image.fromarray(tela).resize((w, h), Image.BILINEAR))


def pintar(rgb, mascara, itens):
    """itens: [{caixa, calor (hxw no recorte) | None, p_ruim, rotulo, cor}]"""
    base = rgb.astype(np.float32)
    base[mascara] = 0.72 * base[mascara] + 0.28 * ex.COR_PLANTA
    for it in itens:
        c, calor = it["caixa"], it.get("calor")
        if calor is None:
            continue
        x0, y0 = int(c["xmin"]), int(c["ymin"])
        h, w = calor.shape
        reg = base[y0:y0 + h, x0:x0 + w]
        planta = mascara[y0:y0 + h, x0:x0 + w]
        quente = (calor >= 0.5) & planta[:reg.shape[0], :reg.shape[1]]
        a = (np.clip((calor - 0.5) / 0.5, 0, 1) * 0.65 * it["p_ruim"])[quente][:, None]
        reg[quente] = (1 - a) * reg[quente] + a * ex.COR_FOCO
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    f = fonte(max(12, img.width // 48))
    for it in itens:
        c = it["caixa"]
        caixa = [c["xmin"], c["ymin"], c["xmax"] - 1, c["ymax"] - 1]
        d.rectangle(caixa, outline=it["cor"], width=max(2, img.width // 320))
        texto = it["rotulo"]
        tw, th = d.textbbox((0, 0), texto, font=f)[2:]
        ty = c["ymin"] - th - 6 if c["ymin"] > th + 6 else c["ymin"]
        d.rectangle([c["xmin"], ty, c["xmin"] + tw + 8, ty + th + 6], fill=it["cor"])
        d.text((c["xmin"] + 4, ty + 3), texto, fill=(255, 255, 255), font=f)
    return img


def cor_da_condicao(condicao, p):
    if condicao is None:
        return (90, 110, 100)
    if condicao == "healthy":
        return (47, 125, 79)
    return (192, 57, 43) if (p or 0) >= 0.6 else (183, 121, 31)
