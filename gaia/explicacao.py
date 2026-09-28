"""
Explicacao visual - a mascara da planta e onde a rede viu problema.

Duas camadas de cor sobre a foto, que vem de lugares DIFERENTES:

  verde     a planta. Sai do indice de excesso de verde (ExG = 2G - R - B) com
            limiar de Otsu. E processamento de imagem classico, nao depende do
            modelo nem de rotulo - por isso e confiavel em foto de campo.

  vermelho  onde a rede se apoiou para dizer 'diseased'. Sai do Grad-CAM da
            cabeca 'condition'. IMPORTANTE: o modelo nunca viu mascara de
            lesao, so o rotulo da imagem inteira. Isto e uma explicacao do que
            a rede olhou, NAO uma segmentacao de lesao medida. Serve para
            auditar a decisao ('olhou para a folha ou para o fundo?'), nao para
            informar area foliar afetada.

O vermelho e recortado pela mascara de planta: calor que cai no fundo e
descartado do foco e reportado a parte, porque atencao no fundo e justamente o
sintoma de que o modelo aprendeu o cenario e nao a lesao.
"""
import numpy as np
import torch
import torch.nn.functional as F

COR_PLANTA = np.array([60, 200, 90], dtype=np.float32)    # verde


COR_FOCO = np.array([235, 60, 45], dtype=np.float32)      # vermelho


COR_FUNDO = np.array([250, 190, 40], dtype=np.float32)    # ambar: calor fora da planta


# ---------------------------------------------------------------- mascara ---
def limiar_otsu(x, bins=256):
    """Otsu em numpy puro - evita depender de opencv/skimage so por isto."""
    hist, bordas = np.histogram(x, bins=bins)
    centros = (bordas[:-1] + bordas[1:]) / 2
    peso1 = np.cumsum(hist)
    peso2 = np.cumsum(hist[::-1])[::-1]
    valido = (peso1 > 0) & (peso2 > 0)
    if not valido.any():
        return float(np.median(x))
    media1 = np.cumsum(hist * centros) / np.maximum(peso1, 1)
    media2 = (np.cumsum((hist * centros)[::-1]) / np.maximum(peso2[::-1], 1))[::-1]
    entre = peso1 * peso2 * (media1 - media2) ** 2
    entre[~valido] = -1
    return float(centros[int(np.argmax(entre))])


def mascara_planta(rgb, fechar=True):
    """rgb uint8 (H,W,3) -> bool (H,W). Excesso de verde + Otsu + preenchimento.

    ExG e o indice de vegetacao classico para camera RGB comum (Woebbecke et
    al., 1995): separa folha de solo, vaso, mao e fundo de laboratorio sem
    treinar nada. O piso em zero evita que, numa foto SEM planta nenhuma, o
    Otsu invente uma metade da imagem como vegetacao.

    O fechamento morfologico nao e cosmetico, e o passo que torna a mascara
    utilizavel AQUI: tecido doente - pustula de ferrugem, necrose, clorose - e
    exatamente a parte menos verde da folha, entao o ExG puro recorta a lesao
    para FORA da planta. Sem isto, a sobreposicao pinta de ambar ("atencao no
    fundo") justamente o acerto do modelo, e a metrica de calor-fora-da-planta
    fica invertida. Fechar e preencher buracos devolve o interior da folha,
    seja ele verde ou nao; o fundo, que nao esta cercado por folha, continua
    de fora.
    """
    x = rgb.astype(np.float32) / 255.0
    soma = x.sum(axis=2) + 1e-6
    r, g, b = (x[..., i] / soma for i in range(3))   # cromaticidade: tira a luz
    exg = 2 * g - r - b
    limiar = max(limiar_otsu(exg), 0.0)
    verde = exg > limiar
    if not fechar or not verde.any():
        return verde

    from scipy import ndimage
    raio = max(2, int(round(min(rgb.shape[:2]) / 45)))
    lado = 2 * raio + 1
    disco = ((np.arange(lado) - raio) ** 2)[:, None] + ((np.arange(lado) - raio) ** 2) <= raio ** 2
    fechada = ndimage.binary_closing(verde, structure=disco)
    return ndimage.binary_fill_holes(fechada)


# --------------------------------------------------------------- grad-cam ---
def camada_alvo(model):
    """O ultimo mapa de features com resolucao espacial, por arquitetura."""
    if hasattr(model, "backbone"):                    # mobilenet / convnext / effnet
        return model.backbone.features
    return model.features                             # poster


class GradCAM:
    """Grad-CAM (Selvaraju et al., 2017) numa cabeca especifica do MultiHead."""

    def __init__(self, model, camada):
        self.model = model
        self.ativacao = None
        self.gradiente = None
        self._hooks = [
            camada.register_forward_hook(self._pegar_ativacao),
            camada.register_full_backward_hook(self._pegar_gradiente),
        ]

    def _pegar_ativacao(self, _mod, _entrada, saida):
        self.ativacao = saida.detach()

    def _pegar_gradiente(self, _mod, _grad_entrada, grad_saida):
        self.gradiente = grad_saida[0].detach()

    def fechar(self):
        for h in self._hooks:
            h.remove()

    def mapa(self, tensor, sensor, eixo, indice, tamanho):
        self.model.zero_grad(set_to_none=True)
        logits = self.model(tensor, sensor)
        logits[eixo][0, indice].backward()

        # peso de cada canal = quanto o score sobe por unidade daquele canal
        peso = self.gradiente.mean(dim=(2, 3), keepdim=True)
        cam = F.relu((peso * self.ativacao).sum(dim=1, keepdim=True))
        cam = F.interpolate(cam, size=tamanho, mode="bilinear", align_corners=False)
        cam = cam[0, 0].cpu().numpy()
        pico = float(cam.max())
        return cam / pico if pico > 0 else cam       # 0..1, ou tudo zero


# --------------------------------------------------------------- oclusao ---
@torch.no_grad()
def oclusao(model, tensor, sensor, eixo, indice, patch=48, passo=16, lote=12):
    """Mapa causal: quanto P(classe) CAI quando cada regiao e apagada.

    Alternativa ao Grad-CAM para quando o gradiente nao serve. Na cabeca
    'condition' do ConvNeXt, treinada ate a saturacao (train 1.000) e com label
    smoothing, o gradiente no ultimo estagio fica pequeno e difuso e o Grad-CAM
    sai blocado - medido nos quatro estagios, nenhum acompanha a lesao. A
    oclusao nao depende disso: mede a resposta do modelo, nao a derivada dela.

    O preenchimento e a propria imagem BORRADA, nao cinza: um retangulo cinza
    cria uma borda dura que o modelo nunca viu no treino, e a queda medida
    passa a ser artefato da borda em vez de efeito do que estava ali.
    """
    _, _, altura, largura = tensor.shape
    borrada = F.avg_pool2d(tensor, 31, stride=1, padding=15)
    base = F.softmax(model(tensor, sensor)[eixo][0], dim=0)[indice].item()

    caixas = [(y, x)
              for y in range(0, max(altura - patch, 0) + 1, passo)
              for x in range(0, max(largura - patch, 0) + 1, passo)]
    mapa = torch.zeros(altura, largura, device=tensor.device)
    contagem = torch.zeros(altura, largura, device=tensor.device)

    i = 0
    while i < len(caixas):
        bloco = caixas[i:i + lote]
        try:
            entrada = tensor.repeat(len(bloco), 1, 1, 1)
            for j, (y, x) in enumerate(bloco):
                entrada[j, :, y:y + patch, x:x + patch] = borrada[0, :, y:y + patch, x:x + patch]
            probs = F.softmax(model(entrada, sensor.repeat(len(bloco), 1))[eixo],
                              dim=1)[:, indice]
        except torch.cuda.OutOfMemoryError:
            # 384px numa placa de notebook nao aguenta lote grande; em vez de
            # abortar, cai pela metade e segue
            if lote == 1:
                raise
            lote = max(1, lote // 2)
            torch.cuda.empty_cache()
            continue
        for j, (y, x) in enumerate(bloco):
            mapa[y:y + patch, x:x + patch] += base - probs[j]
            contagem[y:y + patch, x:x + patch] += 1
        i += len(bloco)

    mapa = (mapa / contagem.clamp(min=1)).clamp(min=0).cpu().numpy()
    pico = float(mapa.max())
    return mapa / pico if pico > 0 else mapa


# ------------------------------------------------------------- composicao ---
def pintar(rgb, planta, calor, forca, limiar_foco=0.5):
    """Compoe as duas camadas sobre a foto original.

    forca escala a opacidade do vermelho pela probabilidade de 'diseased'. O
    Grad-CAM e normalizado pelo proprio pico, entao uma planta sadia tambem tem
    um ponto vermelho vivo se nada segurar a opacidade - seria uma imagem que
    mente. Aqui, sadia = vermelho quase invisivel.
    """
    saida = rgb.astype(np.float32).copy()

    # 1. a planta, tinta leve e uniforme
    saida[planta] = 0.75 * saida[planta] + 0.25 * COR_PLANTA

    # 2. o foco: calor acima do limiar, e so dentro da planta
    quente = calor >= limiar_foco
    foco = quente & planta
    fora = quente & ~planta
    for regiao, cor in ((foco, COR_FOCO), (fora, COR_FUNDO)):
        if not regiao.any():
            continue
        # dentro da regiao, mais calor = mais tinta
        alpha = (np.clip((calor - limiar_foco) / (1 - limiar_foco + 1e-6), 0, 1)
                 * 0.65 * forca)[regiao][:, None]
        saida[regiao] = (1 - alpha) * saida[regiao] + alpha * cor

    return np.clip(saida, 0, 255).astype(np.uint8)


def dividir_transform(composto):
    """Separa a parte geometrica (PIL->PIL) da tensorizacao.

    A avaliacao usa Resize(1.14x) + CenterCrop, ou seja, a rede NAO ve a foto
    inteira. Pintar sobre a foto original deslocaria o mapa em relacao ao que
    se pinta - e o overlay apontaria para o lugar errado. Aplicando so a
    geometria primeiro, o que se exibe e exatamente o que entrou na rede.
    """
    from torchvision import transforms as T
    ops = list(composto.transforms)
    corte = next(i for i, o in enumerate(ops) if isinstance(o, T.ToTensor))
    return T.Compose(ops[:corte]), T.Compose(ops[corte:])
