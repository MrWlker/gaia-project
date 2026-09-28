"""
Servidor do GAIA. Recebe do no de captura, resolve geometria, infere, guarda.

    ./iniciar.sh                      # ou:
    GAIA_MODELO=../modelo uvicorn app:app --host 0.0.0.0 --port 8077

Coordenadas em centimetros a partir do controlador do canteiro (ver
gaia/geometria.py). Esquema: Local, Camera, Sensor, Planta, Observacao,
Leitura e Deteccao.

O QUE ESTE SERVICO FAZ, EM ORDEM
--------------------------------
1. recebe imagem + leituras cruas por canal
2. acha as plantas: regioes da mascara de vegetacao       -> gaia/anotacao.py
3. projeta cada deteccao para (x, y) do canteiro           -> gaia/geometria.py
4. associa cada planta a um id persistente pela POSICAO    -> sem tracking visual
5. interpola os sensores no ponto de cada planta           -> gaia/sensores.py
6. classifica cada recorte com o vetor de sensores daquela planta
7. Grad-CAM da condicao e imagem anotada (caixas + mascara + foco)
8. grava observacao, deteccoes e leituras

O passo 4 e o que transforma fotos avulsas em serie temporal, e e a peca de que
a previsao de trajetoria depende inteiramente.
"""
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        String, create_engine, select)
from sqlalchemy.orm import Session, declarative_base, relationship

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
from gaia import anotacao                  # noqa: E402
from gaia import geometria as geo          # noqa: E402
from gaia import sensores as sensores_mod  # noqa: E402

BASE_MIDIA = os.environ.get("GAIA_MIDIA", "midia")
BANCO = os.environ.get("GAIA_BANCO", "sqlite:///gaia.db")
RUN_CLASSIFICACAO = os.environ.get("GAIA_MODELO", os.environ.get("GAIA_RUN", ""))
RUN_DETECCAO = os.environ.get("GAIA_RUN_DETECCAO", "")

# Raio, em cm, dentro do qual duas deteccoes em dias diferentes sao a MESMA
# planta. Uma alface ocupa uns 25 cm; 20 cm de tolerancia absorve erro de
# trena, de angulo e o balanco da folhagem sem fundir vizinhas.
RAIO_IDENTIDADE_CM = 20.0

Base = declarative_base()


# ---------------------------------------------------------------------------
# esquema
# ---------------------------------------------------------------------------
class Local(Base):
    __tablename__ = "local"
    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)


class Camera(Base):
    __tablename__ = "camera"
    id = Column(Integer, primary_key=True)
    nome = Column(String, unique=True, nullable=False)
    local_id = Column(Integer, ForeignKey("local.id"))
    offset_x = Column(Float, default=0.0)
    offset_y = Column(Float, default=0.0)
    offset_z = Column(Float, default=0.0)
    yaw = Column(Float, default=0.0)
    pitch = Column(Float, default=0.0)
    fov_h = Column(Float, default=62.2)
    largura_px = Column(Integer, default=1640)
    altura_px = Column(Integer, default=1232)
    ativa = Column(Boolean, default=True)

    def para_geometria(self) -> geo.Camera:
        return geo.Camera(self.nome, self.offset_x, self.offset_y, self.offset_z,
                          self.yaw, self.pitch, self.fov_h,
                          self.largura_px, self.altura_px, self.ativa)


class Sensor(Base):
    __tablename__ = "sensor"
    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    local_id = Column(Integer, ForeignKey("local.id"))
    canal = Column(String, nullable=False)       # "soil1" | "temp" | "umid_ar"
    tipo = Column(String, nullable=False)        # SENSOR_FIELDS
    offset_x = Column(Float, default=0.0)
    offset_y = Column(Float, default=0.0)
    offset_z = Column(Float, default=0.0)
    central = Column(Boolean, default=False)
    profundidade_cm = Column(Integer, nullable=True)

    def para_geometria(self) -> geo.Sensor:
        return geo.Sensor(self.nome, self.tipo, self.canal,
                          self.offset_x, self.offset_y, self.offset_z,
                          self.central, self.profundidade_cm)


class Planta(Base):
    """Identidade persistente. NAO e cadastrada a mao: nasce da primeira
    deteccao numa posicao nova e e reencontrada por proximidade nos dias
    seguintes. E o que a camera fixa compra."""
    __tablename__ = "planta"
    id = Column(Integer, primary_key=True)
    local_id = Column(Integer, ForeignKey("local.id"))
    x = Column(Float, nullable=False)            # cm, a partir do controlador
    y = Column(Float, nullable=False)
    especie = Column(String, nullable=True)      # moda das classificacoes
    vista_primeiro = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    vista_ultimo = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    n_observacoes = Column(Integer, default=0)


class Observacao(Base):
    __tablename__ = "observacao"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("camera.id"))
    instante = Column(DateTime, nullable=False)
    caminho = Column(String, nullable=False)     # relativo a BASE_MIDIA
    deteccoes = relationship("Deteccao", back_populates="observacao")


class Leitura(Base):
    """Uma linha por sensor por instante - normalizada desde o inicio, ao
    contrario do SensorData do Spring, que gravava soil1..3 como colunas
    anonimas. Coluna anonima nao sabe a que pedaco do canteiro pertence."""
    __tablename__ = "leitura"
    id = Column(Integer, primary_key=True)
    sensor_id = Column(Integer, ForeignKey("sensor.id"))
    instante = Column(DateTime, nullable=False)
    valor = Column(Float, nullable=False)


class Deteccao(Base):
    __tablename__ = "deteccao"
    id = Column(Integer, primary_key=True)
    observacao_id = Column(Integer, ForeignKey("observacao.id"))
    planta_id = Column(Integer, ForeignKey("planta.id"), nullable=True)
    xmin = Column(Float); ymin = Column(Float)
    xmax = Column(Float); ymax = Column(Float)
    score = Column(Float)
    x_canteiro = Column(Float, nullable=True)
    y_canteiro = Column(Float, nullable=True)
    especie = Column(String, nullable=True)
    p_especie = Column(Float, nullable=True)
    condicao = Column(String, nullable=True)
    p_condicao = Column(Float, nullable=True)
    agente = Column(String, nullable=True)
    p_agente = Column(Float, nullable=True)
    # a leitura ESTIMADA no ponto desta planta, nao a media do canteiro
    temperatura = Column(Float, nullable=True)
    umidade_ar = Column(Float, nullable=True)
    umidade_solo = Column(Float, nullable=True)
    observacao = relationship("Observacao", back_populates="deteccoes")


# ---------------------------------------------------------------------------
motor = create_engine(BANCO, connect_args={"check_same_thread": False}
                      if BANCO.startswith("sqlite") else {})
MODELOS: Dict[str, object] = {}


def carregar_modelos():
    """Carrega uma vez, no startup. Sem run configurado, o servico funciona
    como ingestao pura - util para comecar a coletar ANTES de o modelo final
    existir, que e exatamente a situacao do projeto hoje."""
    if RUN_CLASSIFICACAO and os.path.exists(RUN_CLASSIFICACAO):
        from gaia.preditor import Predictor
        MODELOS["classificador"] = Predictor(RUN_CLASSIFICACAO)
        MODELOS["focador"] = anotacao.Focador(MODELOS["classificador"])
        print("classificador: %s" % RUN_CLASSIFICACAO)
    else:
        print("sem modelo (GAIA_MODELO vazio ou inexistente) - ingestao pura")
    if RUN_DETECCAO and os.path.isdir(RUN_DETECCAO):
        print("detector: %s" % RUN_DETECCAO)


@asynccontextmanager
async def ciclo(_app):
    Base.metadata.create_all(motor)
    os.makedirs(BASE_MIDIA, exist_ok=True)
    carregar_modelos()
    yield


app = FastAPI(title="GAIA", version="0.1", lifespan=ciclo)
os.makedirs(BASE_MIDIA, exist_ok=True)
# as imagens anotadas: a interface da estacao as mostra direto daqui
from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/midia", StaticFiles(directory=BASE_MIDIA), name="midia")


# ---------------------------------------------------------------------------
def identidade_por_posicao(sessao: Session, local_id, x, y, instante) -> Planta:
    """A planta mais proxima dentro do raio, ou uma nova. Sem rastreamento
    visual e sem cadastro manual: a planta nao anda, e a coordenada basta."""
    candidatas = sessao.scalars(select(Planta).where(Planta.local_id == local_id)).all()
    melhor, menor = None, RAIO_IDENTIDADE_CM
    for p in candidatas:
        d = ((p.x - x) ** 2 + (p.y - y) ** 2) ** 0.5
        if d < menor:
            melhor, menor = p, d
    if melhor is None:
        melhor = Planta(local_id=local_id, x=x, y=y,
                        vista_primeiro=instante, n_observacoes=0)
        sessao.add(melhor)
        sessao.flush()
    else:
        # media corrente: a posicao melhora a cada observacao em vez de pular
        n = max(1, melhor.n_observacoes)
        melhor.x = (melhor.x * n + x) / (n + 1)
        melhor.y = (melhor.y * n + y) / (n + 1)
    melhor.vista_ultimo = instante
    melhor.n_observacoes = (melhor.n_observacoes or 0) + 1
    return melhor


def detectar(caminho: str, camera: Camera):
    """Caixas na imagem e a mascara de planta. Sem detector treinado, as caixas
    sao as regioes da mascara de planta (anotacao.py).

    Sem vegetacao, NENHUMA caixa - e nao o quadro inteiro, como era antes.
    Com o piso absoluto de verde, quadro sem vegetacao e quadro sem planta:
    classifica-lo produz "erycina pusilla, doente 81%" para uma lente tampada
    e, pior, cria uma planta falsa na serie temporal daquela posicao."""
    if "detector" in MODELOS:
        raise NotImplementedError("detector carregado mas nao ligado ainda")
    import numpy as np
    from PIL import Image
    with Image.open(caminho) as img:
        rgb = np.asarray(img.convert("RGB"))
    mascara, caixas = anotacao.caixas_pela_mascara(rgb)
    return rgb, mascara, caixas


@app.post("/ingest")
async def ingest(imagem: UploadFile = File(...), meta: str = Form(...),
                 profundidade: Optional[UploadFile] = File(None)):
    """O endpoint que o Pi chama. Guarda a imagem, resolve geometria, infere.

    `profundidade` e opcional: PNG de 16 bits em milimetros, alinhado a cor,
    vindo da RealSense. Fica ao lado da imagem com o sufixo _prof.png, e o meta
    inteiro (com a intrinseca real da camera) ao lado como .json - nada disso
    tem coluna ainda, mas a coleta nao pode esperar o esquema: o que nao se
    guarda hoje nao se recupera depois."""
    try:
        m = json.loads(meta)
    except json.JSONDecodeError as erro:
        raise HTTPException(400, "meta nao e JSON valido: %s" % erro)

    instante = datetime.fromisoformat(m["instante"].replace("Z", "+00:00"))
    with Session(motor) as sessao:
        cam = sessao.scalar(select(Camera).where(Camera.nome == m["camera"]))
        if cam is None:
            raise HTTPException(404, "camera '%s' nao cadastrada - sem extrinseca "
                                     "nao ha como projetar" % m["camera"])

        pasta = os.path.join(BASE_MIDIA, cam.nome, instante.strftime("%Y-%m-%d"))
        os.makedirs(pasta, exist_ok=True)
        destino = os.path.join(pasta, m["imagem"])
        with open(destino, "wb") as fh:
            fh.write(await imagem.read())
        if profundidade is not None:
            with open(os.path.splitext(destino)[0] + "_prof.png", "wb") as fh:
                fh.write(await profundidade.read())
        with open(os.path.splitext(destino)[0] + ".json", "w") as fh:
            json.dump(m, fh, ensure_ascii=False, indent=1)

        obs = Observacao(camera_id=cam.id, instante=instante,
                         caminho=os.path.relpath(destino, BASE_MIDIA))
        sessao.add(obs)
        sessao.flush()

        # leituras cruas -> tabela normalizada, uma linha por sensor
        sensores = sessao.scalars(
            select(Sensor).where(Sensor.local_id == cam.local_id)).all()
        por_canal = {s.canal: s for s in sensores}
        for canal, valor in (m.get("leituras") or {}).items():
            if valor is None or canal not in por_canal:
                continue
            sessao.add(Leitura(sensor_id=por_canal[canal].id,
                               instante=instante, valor=float(valor)))

        cam_geo = cam.para_geometria()
        sens_geo = [s.para_geometria() for s in sensores]
        saida, itens = [], []
        rgb, mascara, caixas = detectar(destino, cam)

        for caixa in caixas:
            u, v = geo.base_da_caixa(caixa["xmin"], caixa["ymin"],
                                     caixa["xmax"], caixa["ymax"])
            ponto = geo.projetar_para_canteiro(cam_geo, u, v)

            det = Deteccao(observacao_id=obs.id, score=caixa.get("score"),
                           **{k: caixa[k] for k in ("xmin", "ymin", "xmax", "ymax")})

            if ponto is None:
                # o raio nao cruza o solo: pitch, cadastro ou caixa no ceu.
                # Guarda a deteccao sem posicao em vez de inventar coordenada.
                sessao.add(det)
                saida.append({"planta": None, "motivo": "fora do plano do solo",
                              "caixa": _caixa_json(caixa)})
                itens.append({"caixa": caixa, "calor": None, "p_ruim": 0,
                              "rotulo": "sem posicao", "cor": (120, 120, 120)})
                continue

            x, y = ponto
            det.x_canteiro, det.y_canteiro = x, y
            planta = identidade_por_posicao(sessao, cam.local_id, x, y, instante)
            det.planta_id = planta.id

            leituras, _diag = sensores_mod.interpolar_no_ponto(
                x, y, sens_geo, m.get("leituras") or {})
            det.temperatura = leituras.get("temperature")
            det.umidade_ar = leituras.get("air_humidity")
            det.umidade_solo = leituras.get("soil_moisture")

            calor, p_ruim, r = None, 0.0, {}
            if "classificador" in MODELOS:
                from PIL import Image
                recorte = Image.fromarray(rgb).crop(
                    (int(caixa["xmin"]), int(caixa["ymin"]),
                     int(caixa["xmax"]), int(caixa["ymax"])))
                r = MODELOS["classificador"].predict(
                    recorte, topk=3, readings=leituras, species=planta.especie)
                p_ruim = next((t["p"] for t in r.get("condition", {}).get("topk", [])
                               if t["label"] == "diseased"), 0.0)
                try:
                    calor = MODELOS["focador"].calor(
                        recorte, leituras, r.get("_reference_species") or planta.especie)
                except Exception as erro:     # anotacao nunca derruba a ingestao
                    print("grad-cam falhou: %s" % erro)
                for eixo, campo, pcampo in (("species", "especie", "p_especie"),
                                            ("condition", "condicao", "p_condicao"),
                                            ("agent", "agente", "p_agente")):
                    if eixo in r and r[eixo].get("trained"):
                        setattr(det, campo, r[eixo]["label"])
                        setattr(det, pcampo, r[eixo]["confidence"])
                if planta.especie is None and det.especie:
                    planta.especie = det.especie

            sessao.add(det)
            extras = {eixo: {"label": r[eixo]["label"], "p": r[eixo]["confidence"]}
                      for eixo in ("hydration", "agent", "organ") if eixo in r}
            saida.append({"planta": planta.id, "x": round(x, 1), "y": round(y, 1),
                          "especie": det.especie, "p_especie": det.p_especie,
                          "condicao": det.condicao, "p_condicao": det.p_condicao,
                          "p_doente": round(p_ruim, 4), "agente": det.agente,
                          "p_agente": det.p_agente, **extras,
                          "temperatura": det.temperatura, "umidade_ar": det.umidade_ar,
                          "umidade_solo": det.umidade_solo,
                          "observacoes": planta.n_observacoes,
                          "caixa": _caixa_json(caixa)})
            nome_curto = (det.especie or "planta").replace("_", " ")
            itens.append({"caixa": caixa, "calor": calor, "p_ruim": p_ruim,
                          "cor": anotacao.cor_da_condicao(det.condicao, det.p_condicao),
                          "rotulo": "#%d %s | %s %.0f%%" % (
                              planta.id, nome_curto[:22], det.condicao or "?",
                              100 * (det.p_condicao or 0))})

        anotada = None
        try:
            img = anotacao.pintar(rgb, mascara, itens)
            caminho_anot = os.path.splitext(destino)[0] + "_anotada.jpg"
            img.save(caminho_anot, quality=88)
            anotada = os.path.relpath(caminho_anot, BASE_MIDIA)
        except Exception as erro:
            print("anotacao falhou: %s" % erro)

        sessao.commit()
        return {"observacao": obs.id, "deteccoes": saida, "anotada": anotada,
                "area_planta": round(float(mascara.mean()), 4),
                "fonte_caixas": caixas[0].get("fonte") if caixas else "sem_vegetacao"}


def _caixa_json(c):
    return {k: round(float(c[k]), 1) for k in ("xmin", "ymin", "xmax", "ymax")}


@app.get("/plantas")
def plantas():
    """As plantas conhecidas e quanto historico cada uma tem. `n_observacoes`
    e o que decide se a cabeca de trajetoria pode opinar: com menos de duas,
    a porta dela fica fechada."""
    with Session(motor) as sessao:
        return [{"id": p.id, "x": p.x, "y": p.y, "especie": p.especie,
                 "n_observacoes": p.n_observacoes,
                 "primeiro": p.vista_primeiro, "ultimo": p.vista_ultimo}
                for p in sessao.scalars(select(Planta)).all()]


@app.get("/planta/{planta_id}/serie")
def serie(planta_id: int):
    """A serie temporal de UMA planta: o que a cabeca de trajetoria vai comer."""
    with Session(motor) as sessao:
        linhas = sessao.scalars(
            select(Deteccao).where(Deteccao.planta_id == planta_id)).all()
        pontos = []
        for d in linhas:
            obs = sessao.get(Observacao, d.observacao_id)
            pontos.append({"instante": obs.instante, "especie": d.especie,
                           "condicao": d.condicao, "p_condicao": d.p_condicao,
                           "temperatura": d.temperatura,
                           "umidade_ar": d.umidade_ar,
                           "umidade_solo": d.umidade_solo})
        pontos.sort(key=lambda p: p["instante"])
        return {"planta": planta_id, "n": len(pontos), "serie": pontos}


@app.get("/saude")
def saude():
    return {"ok": True, "classificador": "classificador" in MODELOS,
            "detector": "detector" in MODELOS, "banco": BANCO}
