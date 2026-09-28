"""
Sensores do no, como plugins. Cada entrada de `sensores` no estacao.json vira
um driver; cada driver devolve {canal: valor}. Trocar de sensor e trocar uma
linha de configuracao, nao codigo.

    {"tipo": "cpu"}                                         temperatura do SoC do Pi
    {"tipo": "realsense"}                                   ASIC e projetor da D435
    {"tipo": "ds18b20", "canal": "temp", "id": "28-0123"}   1-Wire (GPIO4); sem id = o primeiro
    {"tipo": "dht22", "pino": 4, "canal_temp": "temp", "canal_umid": "umid_ar"}
    {"tipo": "mcp3008", "canais": [0,1,2], "seco": {...}, "molhado": {...}}
    {"tipo": "comando", "canal": "temp_estufa", "cmd": "cat /tmp/x"}   stdout -> float
    {"tipo": "simulado", "canal": "temp", "base": 24, "amplitude": 4}

Campos comuns a qualquer entrada, todos opcionais, e usados so pela interface:
    "nome"     rotulo do cartao          "unidade"  "°C", "%" ...
    "min","max" faixa aceitavel - fora dela o cartao acende

O CANAL E O CONTRATO COM O SERVIDOR
-----------------------------------
O no manda canais crus. Quem sabe ONDE o sensor esta e o servidor, na tabela
`sensor` (canal -> posicao no canteiro). Canal que o servidor nao conhece e
ignorado la - por isso `temp_cpu` e `temp_asic` podem ir junto sem poluir o
dado da planta. Para uma leitura entrar na serie da planta, o canal tem de
estar cadastrado no servidor com posicao.

Falha de leitura e None, sempre. Nunca um valor inventado: o par
(valor, flag) do modelo existe exatamente para que 'sem leitura' seja
representavel.
"""
import glob
import math
import random
import subprocess
import threading
import time


class Driver:
    def __init__(self, cfg):
        self.cfg = cfg

    def descrever(self):
        """[(canal, nome, unidade)] - o que este driver produz."""
        raise NotImplementedError

    def ler(self):
        raise NotImplementedError


class CPU(Driver):
    CAMINHO = "/sys/class/thermal/thermal_zone0/temp"

    def descrever(self):
        return [(self.cfg.get("canal", "temp_cpu"), self.cfg.get("nome", "CPU do no"), "°C")]

    def ler(self):
        try:
            with open(self.CAMINHO) as fh:
                return {self.descrever()[0][0]: int(fh.read().strip()) / 1000.0}
        except Exception:
            return {self.descrever()[0][0]: None}


class RealSenseTemp(Driver):
    def __init__(self, cfg, camera=None):
        super().__init__(cfg)
        self.camera = camera

    def descrever(self):
        return [("temp_asic", "ASIC da D435", "°C"),
                ("temp_projetor", "Projetor IR da D435", "°C")]

    def ler(self):
        asic, proj = (None, None) if self.camera is None else self.camera.temperatura_asic()
        return {"temp_asic": asic, "temp_projetor": proj}


class DS18B20(Driver):
    """1-Wire: `dtoverlay=w1-gpio` no config.txt, resistor de 4,7 k entre dados
    e 3,3 V. Leitura por sysfs, sem biblioteca. A prova d'agua vai no solo."""

    def descrever(self):
        return [(self.cfg.get("canal", "temp"), self.cfg.get("nome", "DS18B20"), "°C")]

    def ler(self):
        canal = self.descrever()[0][0]
        ident = self.cfg.get("id")
        caminhos = glob.glob("/sys/bus/w1/devices/%s/w1_slave" % (ident or "28-*"))
        if not caminhos:
            return {canal: None}
        try:
            with open(sorted(caminhos)[0]) as fh:
                linhas = fh.read().splitlines()
            if not linhas[0].strip().endswith("YES"):          # CRC ruim
                return {canal: None}
            valor = int(linhas[1].split("t=")[1]) / 1000.0
            # 85,000 e o valor de power-on-reset: o sensor ainda nao converteu
            return {canal: None if valor == 85.0 else valor}
        except Exception:
            return {canal: None}


class DHT(Driver):
    def __init__(self, cfg):
        super().__init__(cfg)
        from captura import LeitorDHT
        self.leitor = LeitorDHT(cfg.get("pino", 4), cfg.get("modelo", "DHT22"))

    def descrever(self):
        m = self.cfg.get("modelo", "DHT22")
        return [(self.cfg.get("canal_temp", "temp"), "%s temperatura" % m, "°C"),
                (self.cfg.get("canal_umid", "umid_ar"), "%s umidade do ar" % m, "%")]

    def ler(self):
        t, u = self.leitor.ler()
        (ct, _, _), (cu, _, _) = self.descrever()
        return {ct: t, cu: u}


class MCP3008(Driver):
    def __init__(self, cfg):
        super().__init__(cfg)
        from captura import LeitorSolo
        self.leitor = LeitorSolo(cfg.get("canais", [0, 1, 2]),
                                 cfg.get("seco"), cfg.get("molhado"))

    def descrever(self):
        return [("soil%d" % (c + 1), "Solo %d" % (c + 1), "%")
                for c in self.cfg.get("canais", [0, 1, 2])]

    def ler(self):
        return self.leitor.ler()


class Comando(Driver):
    """A porta para o sensor que ainda nao tem driver: qualquer programa que
    imprima um numero. Timeout curto - um script travado nao pode segurar o
    laco de amostragem."""

    def descrever(self):
        return [(self.cfg["canal"], self.cfg.get("nome", self.cfg["canal"]),
                 self.cfg.get("unidade", ""))]

    def ler(self):
        try:
            saida = subprocess.run(self.cfg["cmd"], shell=True, capture_output=True,
                                   text=True, timeout=self.cfg.get("timeout_s", 5))
            return {self.cfg["canal"]: float(saida.stdout.strip().split()[0])}
        except Exception:
            return {self.cfg["canal"]: None}


class Simulado(Driver):
    """Ciclo diario de mentira, com ruido e uma falha de vez em quando - a
    interface precisa mostrar 'sem leitura' tambem."""

    def descrever(self):
        return [(self.cfg["canal"], self.cfg.get("nome", self.cfg["canal"]),
                 self.cfg.get("unidade", "°C"))]

    def ler(self):
        if random.random() < 0.03:
            return {self.cfg["canal"]: None}
        hora = (time.time() % 86400) / 3600.0
        v = (self.cfg.get("base", 24.0)
             + self.cfg.get("amplitude", 4.0) * math.sin((hora - 9) / 24 * 2 * math.pi)
             + random.gauss(0, self.cfg.get("ruido", 0.2)))
        return {self.cfg["canal"]: round(v, 2)}


TIPOS = {"cpu": CPU, "ds18b20": DS18B20, "dht22": DHT, "dht11": DHT,
         "mcp3008": MCP3008, "comando": Comando, "simulado": Simulado}


def faixa(e, canal):
    """min/max de uma entrada. No DHT, `min`/`max` sao da temperatura e
    `min_umid`/`max_umid` da umidade - a mesma faixa nas duas acenderia a
    umidade de 65% como 'acima de 32'."""
    if e.get("tipo") in ("dht11", "dht22") and canal == e.get("canal_umid", "umid_ar"):
        return e.get("min_umid"), e.get("max_umid")
    return e.get("min"), e.get("max")


class Painel:
    """Todos os drivers configurados, com o catalogo que a interface desenha."""

    def __init__(self, entradas, camera=None):
        self.drivers, self.catalogo = [], {}
        self._trava = threading.Lock()
        for e in entradas:
            tipo = e.get("tipo")
            try:
                if tipo == "realsense":
                    d = RealSenseTemp(e, camera)
                elif tipo in ("dht11", "dht22"):
                    d = DHT(dict(e, modelo=tipo.upper()))
                else:
                    d = TIPOS[tipo](e)
            except KeyError:
                print("sensor de tipo desconhecido: %r (tipos: %s)"
                      % (tipo, ", ".join(sorted(TIPOS) + ["realsense"])))
                continue
            except Exception as erro:
                print("sensor %r nao iniciou: %s" % (tipo, erro))
                continue
            self.drivers.append(d)
            for canal, nome, unidade in d.descrever():
                self.catalogo[canal] = {
                    "canal": canal, "tipo": tipo,
                    "nome": e.get("nome", nome) if len(d.descrever()) == 1 else nome,
                    "unidade": e.get("unidade", unidade),
                    "min": faixa(e, canal)[0], "max": faixa(e, canal)[1]}

    def ler(self):
        # o laco de amostragem e a captura leem ao mesmo tempo; o DHT nao
        # aguenta duas leituras simultaneas no mesmo pino
        with self._trava:
            return self._ler()

    def _ler(self):
        saida = {}
        for d in self.drivers:
            try:
                saida.update(d.ler())
            except Exception:
                saida.update({c: None for c, _, _ in d.descrever()})
        return saida
