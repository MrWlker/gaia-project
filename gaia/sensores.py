"""
Vetor de sensores da horta (temperatura, umidade do ar, umidade do solo) e a
tabela de faixas de referencia por especie.

Por que isso existe ja no treino inicial
----------------------------------------
Nenhum dataset publico (PlantVillage, PlantDoc, GBIF) tem leitura de sensor.
Deixar o ramo de sensores para depois obrigaria a *trocar a arquitetura* no dia
do ajuste fino - o checkpoint nao carregaria e o que se chamaria de
"fine-tuning" seria na verdade um treino novo.

A saida e a mesma que o pipeline ja usa para rotulo ausente: **mascara**. Cada
sensor entra como (valor normalizado, flag de presenca). Nas imagens publicas os
flags sao 0 e o ramo devolve exatamente zero (ver SensorEncoder em models.py),
entao o treino inicial e numericamente identico ao modelo so-imagem, mas o
checkpoint ja sai no formato final.

A faixa de referencia por especie
---------------------------------
`data/reference/species_env.csv` traz, para cada cultivo, a faixa em que ele
cresce (temperatura, umidade do ar, umidade do solo). Ela entra de duas formas,
que sao bem diferentes entre si:

1. **Desvio (padrao, seguro).** O que importa agronomicamente nao e "27 C", e
   "6 C acima do que esta especie tolera". O desvio so existe quando ha leitura
   real, entao nas imagens publicas continua mascarado e nao vaza nada.

2. **Preenchimento (`--reference-fill`, opt-in).** Escreve o meio da faixa da
   especie como se fosse leitura. Isso da ao modelo um valor para toda imagem
   publica, mas o valor passa a ser *funcao deterministica do rotulo de
   especie*: e vazamento. Por isso o ramo de sensores alimenta so as cabecas
   `condition` e `hydration` (--sensor-axes), nunca a `species` - que nao pode
   depender da temperatura de hoje de qualquer forma.

Normalizacao
------------
As faixas de normalizacao abaixo sao FIXAS e fisicas, nao estatisticas do
conjunto de treino. Isso e proposital: como o treino inicial nao tem nenhuma
leitura, nao ha estatistica para calcular, e uma leitura futura de 27 C precisa
virar sempre o mesmo numero - hoje e daqui a um ano.
"""
import csv
import os
from typing import Dict, List, Optional, Sequence, Tuple

# A tabela de faixas por especie NAO vem neste repositorio (so codigo): ela e
# publicada junto do modelo, e o preditor a acha na pasta dele. Sem a tabela,
# o desvio da faixa entra como ausente (flag 0) e a inferencia segue.
REFERENCE_CSV = os.environ.get("GAIA_REFERENCIA", "")

# nome da coluna no manifesto -> (minimo, maximo) em unidade fisica
SENSOR_RANGES = (
    ("temperature",   0.0,  50.0),   # C     - faixa util do DHT11
    ("air_humidity",  0.0, 100.0),   # %UR   - DHT11
    ("soil_moisture", 0.0, 100.0),   # %     - sensor de solo da zona da planta
)

SENSOR_FIELDS: Tuple[str, ...] = tuple(name for name, _, _ in SENSOR_RANGES)

# leituras + flags de leitura + desvios da faixa da especie + flags de desvio
SENSOR_DIM = 4 * len(SENSOR_FIELDS)

# desvio em "meias-larguras da faixa": 0 = centro do otimo, +-1 = borda. Cortado
# em +-DEV_CLIP para que um sensor solto (leitura absurda) nao domine o vetor.
DEV_CLIP = 3.0


def normalize(name: str, value: float) -> float:
    """Unidade fisica -> [0, 1], com corte nas pontas."""
    for field, lo, hi in SENSOR_RANGES:
        if field == name:
            return min(1.0, max(0.0, (float(value) - lo) / (hi - lo)))
    raise KeyError(name)


def denormalize(name: str, value: float) -> float:
    for field, lo, hi in SENSOR_RANGES:
        if field == name:
            return lo + float(value) * (hi - lo)
    raise KeyError(name)


def _is_missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip().lower() in ("nan", "none", "null")
    try:
        return value != value  # NaN
    except Exception:
        return True


def _as_float(value) -> Optional[float]:
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# tabela de referencia por especie
# ---------------------------------------------------------------------------

_REFERENCE_CACHE: Optional[Dict[str, Dict[str, Tuple[float, float]]]] = None


def load_reference(path: Optional[str] = None) -> Dict[str, Dict[str, Tuple[float, float]]]:
    """{especie: {campo: (min, max)}}. Especie ausente = sem referencia."""
    global _REFERENCE_CACHE
    if _REFERENCE_CACHE is not None:
        return _REFERENCE_CACHE
    table: Dict[str, Dict[str, Tuple[float, float]]] = {}
    path = path or REFERENCE_CSV
    if path and os.path.isfile(path):
        with open(path, newline="") as fh:
            # o arquivo comeca com comentarios explicando de onde vem cada faixa
            reader = csv.DictReader(line for line in fh if not line.startswith("#"))
            for row in reader:
                species = (row.get("species") or "").strip().lower()
                if not species:
                    continue
                ranges = {}
                for field in SENSOR_FIELDS:
                    lo = _as_float(row.get(field + "_min"))
                    hi = _as_float(row.get(field + "_max"))
                    if lo is not None and hi is not None and hi > lo:
                        ranges[field] = (lo, hi)
                if ranges:
                    table[species] = ranges
    _REFERENCE_CACHE = table
    return table


def usar_referencia(path: str) -> None:
    """Aponta a tabela de faixas (species_env.csv) e descarta o cache."""
    global REFERENCE_CSV, _REFERENCE_CACHE
    REFERENCE_CSV, _REFERENCE_CACHE = path, None


def reference_for(species: Optional[str]) -> Dict[str, Tuple[float, float]]:
    if not species:
        return {}
    return load_reference().get(str(species).strip().lower(), {})


def reference_midpoint(species: Optional[str]) -> Dict[str, float]:
    """Meio da faixa da especie - o que --reference-fill grava no manifesto."""
    return {field: (lo + hi) / 2.0 for field, (lo, hi) in reference_for(species).items()}


def _deviation(value: float, lo: float, hi: float) -> float:
    """(leitura - centro) / meia-largura, cortado e reescalado para [-1, 1]."""
    center = (lo + hi) / 2.0
    half = (hi - lo) / 2.0
    dev = (value - center) / half
    return max(-DEV_CLIP, min(DEV_CLIP, dev)) / DEV_CLIP


# ---------------------------------------------------------------------------
# vetor de entrada
# ---------------------------------------------------------------------------

def encode(readings: Optional[Dict[str, float]], species: Optional[str] = None) -> List[float]:
    """dict de leituras -> vetor de SENSOR_DIM posicoes, em quatro blocos:

        [valores] [flags de valor] [desvios da faixa] [flags de desvio]

    Sensor ausente entra como 0.0 com flag 0.0. O par (valor, flag) e o que
    impede a rede de confundir "0 C" com "sem leitura". O desvio so aparece
    quando ha leitura *e* a especie tem faixa de referencia.
    """
    readings = readings or {}
    ranges = reference_for(species)
    values, mask, devs, dev_mask = [], [], [], []
    for field in SENSOR_FIELDS:
        raw = _as_float(readings.get(field))
        if raw is None:
            values.append(0.0)
            mask.append(0.0)
            devs.append(0.0)
            dev_mask.append(0.0)
            continue
        values.append(normalize(field, raw))
        mask.append(1.0)
        if field in ranges:
            devs.append(_deviation(raw, *ranges[field]))
            dev_mask.append(1.0)
        else:
            devs.append(0.0)
            dev_mask.append(0.0)
    return values + mask + devs + dev_mask


def decode(vector: Sequence[float]) -> Dict[str, Optional[float]]:
    """Inverso parcial de encode(), para log e depuracao."""
    n = len(SENSOR_FIELDS)
    return {field: denormalize(field, vector[i]) if vector[n + i] > 0.5 else None
            for i, field in enumerate(SENSOR_FIELDS)}


def any_present(vector: Sequence[float]) -> bool:
    n = len(SENSOR_FIELDS)
    return any(v > 0.5 for v in vector[n:2 * n])


# ---------------------------------------------------------------------------
# leitura POR PLANTA, a partir de varios sensores espalhados
# ---------------------------------------------------------------------------
# O `encode` acima recebe UMA leitura por grandeza, como se o canteiro inteiro
# tivesse uma temperatura so. Nao tem: e o ponto de espalhar sensores. O que
# falta e o passo anterior - dada a posicao da planta e a posicao dos sensores,
# estimar a leitura NAQUELE ponto.
#
# A escolha aqui e deliberadamente a mais simples que usa a geometria:
# ponderacao inversa da distancia (Shepard, 1968). Nao e kriging, nao estima
# incerteza espacial, e assume que a grandeza varia suavemente no canteiro -
# o que vale para umidade de solo em poucos metros e vale menos para
# temperatura perto de uma parede ensolarada.
#
# A vantagem que decidiu a escolha nao e estatistica, e arquitetural: o
# resultado colapsa N sensores + extrinsecas nos MESMOS SENSOR_DIM valores que
# o modelo ja consome. A rede nao muda, o checkpoint continua valido, e o
# ajuste fino continua sendo ajuste fino. Um encoder de conjunto sobre os
# sensores (posicao relativa como token) e o passo seguinte, e ai a
# arquitetura muda de verdade - fazer isso antes de ter serie temporal seria
# trocar o formato do checkpoint para ganhar nada.

# expoente da ponderacao. 2 e o usual; maior aproxima do "vizinho mais
# proximo", menor aproxima da media simples do canteiro.
IDW_POTENCIA = 2.0

# raio, em cm, dentro do qual um sensor e considerado "em cima" da planta e
# leva todo o peso. Evita divisao por zero e evita que 1 cm de erro de trena
# vire diferenca de peso gigante.
IDW_RAIO_MINIMO = 5.0

# peso fixo do sensor `central`, que descreve o canteiro inteiro e nao um
# ponto. Entra como piso: mesmo longe de qualquer sensor local, a planta herda
# o ambiente do canteiro em vez de ficar sem leitura.
PESO_CENTRAL = 0.25


def interpolar_no_ponto(x: float, y: float, sensores, leituras_por_canal,
                        potencia: float = IDW_POTENCIA,
                        raio_minimo: float = IDW_RAIO_MINIMO,
                        peso_central: float = PESO_CENTRAL):
    """Leitura estimada em (x, y) a partir dos sensores em volta.

    `sensores`          : iteravel de objetos com .tipo, .canal, .central e
                          .distancia_ate(x, y)  (ver gaia/geometria.py)
    `leituras_por_canal`: {canal: valor} do instante da foto. Canal ausente ou
                          None = sensor sem leitura, e ele simplesmente nao
                          entra na conta.

    Devolve (leituras, diagnostico):
      leituras    {campo: valor ou None} - pronto para o `encode`
      diagnostico {campo: {...}} com quem contribuiu, com que peso, e a que
                  distancia estava o mais proximo. Existe para a bancada poder
                  mostrar, e para nao acreditar num numero sem saber de onde
                  veio.
    """
    leituras: Dict[str, Optional[float]] = {}
    diagnostico: Dict[str, Dict] = {}

    for campo in SENSOR_FIELDS:
        contribuicoes = []
        for s in sensores:
            if getattr(s, "tipo", None) != campo:
                continue
            valor = _as_float((leituras_por_canal or {}).get(getattr(s, "canal", None)))
            if valor is None:
                continue
            if getattr(s, "central", False):
                peso, dist = peso_central, None
            else:
                dist = s.distancia_ate(x, y)
                peso = 1.0 / (max(dist, raio_minimo) ** potencia)
            contribuicoes.append((getattr(s, "nome", "?"), valor, peso, dist))

        if not contribuicoes:
            leituras[campo] = None
            diagnostico[campo] = {"fonte": [], "n": 0, "mais_proximo_cm": None}
            continue

        total = sum(c[2] for c in contribuicoes)
        estimado = sum(v * p for _, v, p, _ in contribuicoes) / total
        locais = [c[3] for c in contribuicoes if c[3] is not None]
        leituras[campo] = estimado
        diagnostico[campo] = {
            "valor": estimado,
            "n": len(contribuicoes),
            "mais_proximo_cm": min(locais) if locais else None,
            "fonte": [{"sensor": n, "valor": v, "peso": p / total,
                       "distancia_cm": d}
                      for n, v, p, d in sorted(contribuicoes, key=lambda c: -c[2])],
        }
    return leituras, diagnostico


def encode_espacial(x: float, y: float, sensores, leituras_por_canal,
                    species: Optional[str] = None, **kwargs):
    """Atalho: interpola no ponto da planta e devolve o vetor que o modelo come.

    Devolve (vetor, diagnostico). O vetor tem exatamente SENSOR_DIM posicoes -
    e o mesmo contrato de `encode`, de proposito.
    """
    leituras, diagnostico = interpolar_no_ponto(x, y, sensores,
                                                leituras_por_canal, **kwargs)
    return encode(leituras, species), diagnostico
