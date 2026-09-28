"""
Arquitetura do modelo GAIA - so o necessario para CARREGAR os pesos publicados.

Uma rede multi-cabeca: um backbone da torchvision (ConvNeXt-Base no modelo
publicado) extrai features da imagem; um ramo pequeno codifica as leituras de
sensor (temperatura, umidade do ar, umidade do solo) e as soma as features; e
cabecas independentes dizem especie, condicao, hidratacao, agente e orgao.

O codigo de treino nao faz parte deste repositorio: ele acompanha o artigo.
Aqui a rede e construida vazia (pretrained=False) e os pesos vem do checkpoint.
"""
from typing import Dict, Optional, Sequence

import torch
import torch.nn as nn

from . import sensores as sensors_mod

class SensorEncoder(nn.Module):
    """Ramo de metadados: (temperatura, umidade do ar, umidade do solo) -> vetor
    somado as features da imagem.

    Duas decisoes que fazem esse ramo poder existir ja no treino inicial, em que
    NENHUMA imagem tem leitura de sensor:

    1. **Projecao final zerada.** A ultima Linear comeca com peso e vies zero,
       entao no primeiro passo o ramo contribui exatamente 0 e o modelo e
       numericamente identico ao so-imagem. O ramo abre sozinho, por gradiente,
       quando (e so quando) o sensor passar a informar algo util.
    2. **Porta pela mascara.** Se a amostra nao tem nenhuma leitura, a saida e
       multiplicada por 0. Sem isso, o vies das camadas internas vazaria um
       valor constante para toda imagem publica - a rede aprenderia um "sensor
       fantasma" que nao existe.

    O resultado pratico: o checkpoint do treino inicial ja tem o formato final.
    O ajuste fino com dados reais da horta e um ajuste fino de verdade, e nao
    uma arquitetura nova disfarcada.
    """

    def __init__(self, out_features: int, hidden: int = 64,
                 in_features: int = sensors_mod.SENSOR_DIM):
        super().__init__()
        self.n_fields = len(sensors_mod.SENSOR_FIELDS)
        self.net = nn.Sequential(
            nn.Linear(in_features, hidden),
            nn.ReLU(inplace=True),
            nn.Linear(hidden, hidden),
            nn.ReLU(inplace=True),
            nn.Linear(hidden, out_features),
        )
        nn.init.zeros_(self.net[-1].weight)
        nn.init.zeros_(self.net[-1].bias)

    def forward(self, sensor: torch.Tensor) -> torch.Tensor:
        # flags de presenca ocupam a segunda metade do vetor
        present = (sensor[:, self.n_fields:].sum(dim=1, keepdim=True) > 0).float()
        return self.net(sensor) * present


class SensorFusion(nn.Module):
    """Backbone de imagem + SensorEncoder, somados antes das cabecas."""

    def __init__(self, in_features: int, use_sensors: bool = True):
        super().__init__()
        self.encoder = SensorEncoder(in_features) if use_sensors else None

    def forward(self, feats: torch.Tensor,
                sensor: Optional[torch.Tensor]) -> Optional[torch.Tensor]:
        if self.encoder is None or sensor is None:
            return None
        return feats + self.encoder(sensor)


# Cabecas que enxergam o vetor de sensores. 'species' e 'category' ficam de fora
# de proposito: a identidade da planta nao depende da temperatura de hoje, e,
# se as leituras vierem da tabela de referencia por especie (--reference-fill),
# elas seriam funcao deterministica do proprio rotulo de especie - vazamento.
# Sanidade, hidratacao e agente causador sao os eixos em que o ambiente de fato
# entra: fungo e oomiceto proliferam com umidade alta e folha molhada, e e essa
# dependencia que justifica o sensor alimentar tambem a cabeca 'agent'.
DEFAULT_SENSOR_AXES = ("condition", "hydration", "agent")


class MultiHead(nn.Module):
    """Uma camada linear por eixo de rotulo (species / condition / category /
    hydration / agent). Cada cabeca le as features da imagem ou as features
    fundidas com os sensores, conforme sensor_axes."""

    def __init__(self, in_features: int, head_sizes: Dict[str, int],
                 dropout: float = 0.0,
                 sensor_axes: Sequence[str] = DEFAULT_SENSOR_AXES):
        super().__init__()
        self.dropout = nn.Dropout(dropout) if dropout > 0 else nn.Identity()
        self.sensor_axes = tuple(a for a in sensor_axes if a in head_sizes)
        self.heads = nn.ModuleDict({
            name: nn.Linear(in_features, size) for name, size in head_sizes.items()
        })

    def forward(self, feats: torch.Tensor,
                fused: Optional[torch.Tensor] = None) -> Dict[str, torch.Tensor]:
        image_only = self.dropout(feats)
        with_sensor = self.dropout(fused) if fused is not None else image_only
        return {
            name: head(with_sensor if name in self.sensor_axes else image_only)
            for name, head in self.heads.items()
        }


class PretrainedBackbone(nn.Module):
    """Backbone da torchvision com pesos ImageNet e o mesmo MultiHead no topo."""

    input_size = 224

    def __init__(self, arch: str, head_sizes: Dict[str, int],
                 pretrained: bool = True, freeze_backbone: bool = False,
                 dropout: float = 0.2, use_sensors: bool = True,
                 sensor_axes: Sequence[str] = DEFAULT_SENSOR_AXES,
                 input_size: Optional[int] = None):
        super().__init__()
        from torchvision import models as tvm

        if input_size:
            self.input_size = input_size

        if arch == "mobilenet":
            weights = tvm.MobileNet_V3_Small_Weights.IMAGENET1K_V1 if pretrained else None
            net = tvm.mobilenet_v3_small(weights=weights)
            in_features = net.classifier[0].in_features
            net.classifier = nn.Identity()
        elif arch == "efficientnet":
            weights = tvm.EfficientNet_B0_Weights.IMAGENET1K_V1 if pretrained else None
            net = tvm.efficientnet_b0(weights=weights)
            in_features = net.classifier[1].in_features
            net.classifier = nn.Identity()
        elif arch == "efficientnetv2":
            weights = tvm.EfficientNet_V2_S_Weights.IMAGENET1K_V1 if pretrained else None
            net = tvm.efficientnet_v2_s(weights=weights)
            in_features = net.classifier[1].in_features
            net.classifier = nn.Identity()
        elif arch == "convnext":
            # o modelo "grande" do experimento: e o que justifica a A100 80GB
            weights = tvm.ConvNeXt_Base_Weights.IMAGENET1K_V1 if pretrained else None
            net = tvm.convnext_base(weights=weights)
            in_features = net.classifier[2].in_features
            net.classifier = nn.Sequential(net.classifier[0], net.classifier[1])
        else:
            raise ValueError("arquitetura desconhecida: %s" % arch)

        self.backbone = net
        if freeze_backbone:
            for p in self.backbone.parameters():
                p.requires_grad = False
        self.fusion = SensorFusion(in_features, use_sensors=use_sensors)
        self.head = MultiHead(in_features, head_sizes, dropout=dropout,
                              sensor_axes=sensor_axes)

    def forward(self, x, sensor=None):
        feats = self.backbone(x)
        return self.head(feats, self.fusion(feats, sensor))

    def unfreeze(self):
        for p in self.backbone.parameters():
            p.requires_grad = True


MODEL_NAME = "GAIA"


ARCHITECTURES = ("mobilenet", "efficientnet", "efficientnetv2", "convnext")


_DISPLAY = {"mobilenet": "MobileNet", "efficientnet": "EfficientNet",
            "efficientnetv2": "EfficientNetV2", "convnext": "ConvNeXt"}


def model_id(arch: str) -> str:
    """Nome de exibicao: GAIA-MobileNet, GAIA-ConvNeXt, ..."""
    return "%s-%s" % (MODEL_NAME, _DISPLAY.get(arch, arch))


def build_model(name: str, head_sizes: Dict[str, int], **kwargs) -> nn.Module:
    use_sensors = kwargs.get("use_sensors", True)
    sensor_axes = kwargs.get("sensor_axes", DEFAULT_SENSOR_AXES)
    input_size = kwargs.get("input_size")
    if name in ARCHITECTURES:
        return PretrainedBackbone(
            name, head_sizes,
            pretrained=kwargs.get("pretrained", True),
            freeze_backbone=kwargs.get("freeze_backbone", False),
            use_sensors=use_sensors, sensor_axes=sensor_axes,
            input_size=input_size,
        )
    raise ValueError("modelo desconhecido: %s" % name)
