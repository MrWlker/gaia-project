"""
Geometria do canteiro: pixel <-> posicao no mundo.

O controlador e a origem. Toda camera e todo sensor se descrevem por
deslocamento a partir dele, em centimetros - e o mesmo sistema que
`Camera.java` e `Sensor.java` ja gravam no banco.

POR QUE ISTO E O EIXO DO SISTEMA
--------------------------------
Sem posicao, uma deteccao e uma caixa numa foto. Com posicao, ela e uma planta
no canteiro, e tres coisas passam a ser a mesma pergunta:

  - qual sensor descreve o ambiente DESTA planta        (peso por distancia)
  - a deteccao da camera B e a mesma planta da camera A (mesma coordenada)
  - a deteccao de amanha e a mesma planta de hoje       (mesma coordenada)

A terceira e a identidade persistente de que a previsao de trajetoria depende,
e sai de graca porque a camera e fixa: a planta nao anda. Nao ha rastreamento
visual aqui, e nao deve haver.

CONVENCAO DE EIXOS (unica, e vale para o banco tambem)
------------------------------------------------------
    x  cresce para a DIREITA de quem olha o canteiro de frente
    y  cresce para a FRENTE (afastando-se do observador)
    z  cresce para CIMA; z = 0 e a superficie do solo
    unidade: centimetro

    yaw   graus, 0 = camera aponta para +y, positivo gira em direcao a +x
    pitch graus, 0 = horizonte, NEGATIVO olha para baixo

Camera de horta fica acima e aponta para baixo, entao o pitch tipico e
negativo (-30, -45). Um pitch >= 0 nunca cruza o solo a frente, e a projecao
devolve None em vez de inventar um ponto - erro de cadastro tem de aparecer.

PRECISAO, HONESTAMENTE
----------------------
Isto e um modelo pinhole sem distorcao, alimentado por medida de trena e
transferidor. Serve para associar planta a sensor e para reidentificar planta
entre dias, que e o uso. NAO serve para fenotipagem metrica (area foliar em
cm2, altura absoluta): para isso e preciso calibrar com padrao xadrez e
estimar a distorcao radial. Quando isso for feito, o lugar de guardar e o
mesmo - `Camera`, com campos novos.
"""
import math
import os
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class Camera:
    """Espelha `Camera.java`. Angulos em graus, distancias em centimetros."""

    def __init__(self, nome: str, x: float, y: float, z: float,
                 yaw: float, pitch: float, fov_h: float,
                 largura_px: int, altura_px: int, ativa: bool = True):
        self.nome = nome
        self.x, self.y, self.z = float(x), float(y), float(z)
        self.yaw, self.pitch = float(yaw), float(pitch)
        self.fov_h = float(fov_h)
        self.largura_px, self.altura_px = int(largura_px), int(altura_px)
        self.ativa = ativa

    # ---- base ortonormal da camera, em coordenadas do mundo ----------------
    def _base(self) -> Tuple[Tuple[float, float, float], ...]:
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        frente = (sy * cp, cy * cp, sp)
        direita = (cy, -sy, 0.0)                       # horizontal por construcao
        baixo = (                                       # frente x direita
            frente[1] * direita[2] - frente[2] * direita[1],
            frente[2] * direita[0] - frente[0] * direita[2],
            frente[0] * direita[1] - frente[1] * direita[0],
        )
        return frente, direita, baixo

    @property
    def foco_px(self) -> float:
        """Distancia focal em pixels, derivada do campo de visao horizontal."""
        return (self.largura_px / 2.0) / math.tan(math.radians(self.fov_h) / 2.0)

    def __repr__(self):
        return ("Camera(%s, pos=(%.0f,%.0f,%.0f), yaw=%.0f, pitch=%.0f, fov=%.0f)"
                % (self.nome, self.x, self.y, self.z, self.yaw, self.pitch, self.fov_h))


class Sensor:
    """Espelha `Sensor.java`."""

    def __init__(self, nome: str, tipo: str, canal: str,
                 x: float, y: float, z: float,
                 central: bool = False, profundidade_cm: Optional[int] = None):
        self.nome, self.tipo, self.canal = nome, tipo, canal
        self.x, self.y, self.z = float(x), float(y), float(z)
        self.central = bool(central)
        self.profundidade_cm = profundidade_cm

    def distancia_ate(self, x: float, y: float) -> float:
        """Distancia no PLANO. A profundidade do sensor de solo nao entra: dois
        sensores no mesmo ponto a 5 e a 15 cm descrevem a mesma planta, e a
        diferenca entre eles e informacao de perfil, nao de distancia."""
        return math.hypot(self.x - x, self.y - y)

    def __repr__(self):
        return ("Sensor(%s, %s, pos=(%.0f,%.0f,%.0f)%s)"
                % (self.nome, self.tipo, self.x, self.y, self.z,
                   ", central" if self.central else ""))


# ---------------------------------------------------------------------------
# pixel -> canteiro
# ---------------------------------------------------------------------------
def projetar_para_canteiro(camera: Camera, u: float, v: float,
                           z_plano: float = 0.0) -> Optional[Tuple[float, float]]:
    """Pixel (u, v) -> (x, y) no canteiro, em cm a partir do controlador.

    `z_plano` e a altura do plano onde se quer cair. O padrao 0 e o solo, que e
    onde esta a BASE da planta - use o pixel da base da caixa de deteccao
    (centro horizontal, borda inferior), nao o centro da caixa: o centro flutua
    com a folhagem e a altura, a base fica no solo.

    Devolve None quando o raio nao cruza o plano a frente da camera: pitch para
    cima, camera abaixo do plano, ou pixel acima da linha do horizonte. None e
    resposta legitima e tem de ser tratada - nunca substituida por um chute.
    """
    frente, direita, baixo = camera._base()
    f = camera.foco_px
    dx = u - camera.largura_px / 2.0
    dy = v - camera.altura_px / 2.0

    d = (f * frente[0] + dx * direita[0] + dy * baixo[0],
         f * frente[1] + dx * direita[1] + dy * baixo[1],
         f * frente[2] + dx * direita[2] + dy * baixo[2])

    if abs(d[2]) < 1e-12:
        return None                                   # raio paralelo ao plano
    t = (z_plano - camera.z) / d[2]
    if t <= 0:
        return None                                   # o plano fica atras
    return (camera.x + t * d[0], camera.y + t * d[1])


def projetar_para_imagem(camera: Camera, x: float, y: float,
                         z: float = 0.0) -> Optional[Tuple[float, float]]:
    """(x, y, z) no canteiro -> pixel (u, v). O caminho de volta.

    Serve para saber QUAIS cameras enxergam uma planta ja conhecida - o passo
    que amarra a mesma planta em duas vistas, e que permite recortar a mesma
    planta em todas as cameras que a veem.

    Devolve None se o ponto esta atras da camera. Fora do quadro NAO e None: o
    pixel devolvido pode cair fora de [0, largura] x [0, altura], e quem chama
    decide - `esta_no_quadro` faz esse teste.
    """
    frente, direita, baixo = camera._base()
    p = (x - camera.x, y - camera.y, z - camera.z)
    pz = p[0] * frente[0] + p[1] * frente[1] + p[2] * frente[2]
    if pz <= 1e-9:
        return None                                   # atras da camera
    px = p[0] * direita[0] + p[1] * direita[1] + p[2] * direita[2]
    py = p[0] * baixo[0] + p[1] * baixo[1] + p[2] * baixo[2]
    f = camera.foco_px
    return (camera.largura_px / 2.0 + f * px / pz,
            camera.altura_px / 2.0 + f * py / pz)


def esta_no_quadro(camera: Camera, x: float, y: float, z: float = 0.0,
                   margem_px: float = 0.0) -> bool:
    uv = projetar_para_imagem(camera, x, y, z)
    if uv is None:
        return False
    u, v = uv
    return (-margem_px <= u <= camera.largura_px + margem_px
            and -margem_px <= v <= camera.altura_px + margem_px)


def cameras_que_veem(cameras: Iterable[Camera], x: float, y: float,
                     z: float = 0.0) -> List[Camera]:
    """As cameras ativas que enxergam este ponto. Duas ou mais = a planta tem
    vista multipla, e o fundo deixa de ser sempre o mesmo - que e o argumento
    medido para varias cameras (ver `teste_fundo.py`)."""
    return [c for c in cameras if c.ativa and esta_no_quadro(c, x, y, z)]


def base_da_caixa(xmin: float, ymin: float, xmax: float,
                  ymax: float) -> Tuple[float, float]:
    """O pixel a projetar, dada uma caixa de deteccao: centro horizontal na
    borda inferior. E onde a planta encosta no solo."""
    return ((xmin + xmax) / 2.0, ymax)


# ---------------------------------------------------------------------------
# autoteste - roda com: python3 -m gaia.geometria
# ---------------------------------------------------------------------------
def _autoteste() -> int:
    falhas = 0

    def checa(nome, cond, extra=""):
        nonlocal falhas
        print("  [%s] %s%s" % ("ok " if cond else "FALHA", nome,
                               "  " + extra if extra else ""))
        falhas += (not cond)

    # camera a 150 cm de altura, 100 cm atras da origem, olhando para frente e
    # 45 graus para baixo
    cam = Camera("frontal", x=0, y=-100, z=150, yaw=0, pitch=-45,
                 fov_h=62.2, largura_px=640, altura_px=480)
    print(cam)

    centro = projetar_para_canteiro(cam, 320, 240)
    checa("centro do quadro cai no solo a frente", centro is not None and centro[1] > cam.y,
          "-> %s" % (None if centro is None else "(%.1f, %.1f)" % centro))
    checa("centro nao desvia no eixo x", abs(centro[0]) < 1e-6, "x=%.6f" % centro[0])
    # a 45 graus, o alcance horizontal ate o solo e igual a altura
    checa("alcance = altura a 45 graus", abs((centro[1] - cam.y) - cam.z) < 1e-6,
          "%.2f vs %.2f" % (centro[1] - cam.y, cam.z))

    # ida e volta: projetar e desprojetar tem de fechar
    maior = 0.0
    for u in (60, 320, 580):
        for v in (260, 360, 470):
            p = projetar_para_canteiro(cam, u, v)
            if p is None:
                continue
            uv = projetar_para_imagem(cam, p[0], p[1], 0.0)
            maior = max(maior, math.hypot(uv[0] - u, uv[1] - v))
    checa("ida e volta fecha em < 1e-6 px", maior < 1e-6, "erro max %.2e px" % maior)

    # O topo do quadro so deixa de cruzar o solo quando passa do horizonte, e
    # isso depende do pitch E do campo de visao vertical. Com pitch -45 e meio
    # FOV vertical de ~24 graus, o topo ainda aponta 20 graus para baixo: tem
    # de cruzar, e longe. Errar isso e o modo classico de "consertar" a
    # projecao para satisfazer um teste que estava errado.
    meio_v = math.degrees(math.atan((cam.altura_px / 2.0) / cam.foco_px))
    topo = projetar_para_canteiro(cam, 320, 0)
    checa("topo do quadro ainda aponta para baixo (%.1f graus)" % (cam.pitch + meio_v),
          cam.pitch + meio_v < 0)
    checa("topo do quadro cruza o solo, e mais longe que o centro",
          topo is not None and topo[1] > centro[1],
          "y_topo=%.0f  vs  y_centro=%.0f" % (topo[1], centro[1]))

    # camera quase horizontal: ai sim o topo passa do horizonte
    rasa = Camera("rasa", 0, -100, 150, yaw=0, pitch=-10, fov_h=62.2,
                  largura_px=640, altura_px=480)
    checa("com pitch -10 o topo do quadro passa do horizonte e devolve None",
          projetar_para_canteiro(rasa, 320, 0) is None)

    # pitch para cima nunca cruza
    cima = Camera("errada", 0, 0, 150, yaw=0, pitch=10, fov_h=62.2,
                  largura_px=640, altura_px=480)
    checa("pitch positivo devolve None", projetar_para_canteiro(cima, 320, 240) is None)

    # yaw: girando 90 graus, o centro passa a cair em +x
    lado = Camera("lateral", 0, 0, 150, yaw=90, pitch=-45, fov_h=62.2,
                  largura_px=640, altura_px=480)
    c2 = projetar_para_canteiro(lado, 320, 240)
    checa("yaw=90 leva o centro para +x", c2 is not None and c2[0] > 0 and abs(c2[1]) < 1e-6,
          "-> (%.1f, %.1f)" % c2)

    # direita da imagem = +x quando yaw=0
    dir_ = projetar_para_canteiro(cam, 600, 240)
    checa("pixel a direita cai em x positivo", dir_[0] > 0, "x=%.1f" % dir_[0])

    # duas cameras veem a mesma planta
    cam2 = Camera("lateral2", x=120, y=0, z=150, yaw=-60, pitch=-45,
                  fov_h=62.2, largura_px=640, altura_px=480)
    veem = cameras_que_veem([cam, cam2], centro[0], centro[1])
    checa("ponto central e visto por >= 1 camera", len(veem) >= 1,
          "vistas: %s" % [c.nome for c in veem])

    print("\n%s" % ("todos os testes passaram" if not falhas
                    else "%d FALHA(S)" % falhas))
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(_autoteste())
