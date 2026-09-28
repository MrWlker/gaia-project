#!/usr/bin/env python3
"""
Estacao do GAIA no Raspberry Pi 3: RealSense D435 + sensores + interface web.

    python3 estacao.py --config estacao.json              # hardware real
    python3 estacao.py --config estacao.json --simular    # sem camera nem sensores

    interface:  http://<ip-do-pi>:8080

O QUE RODA ONDE
---------------
    Pi 3 (1 GB)            notebook (hotspot, GPU)
    -----------            -----------------------
    D435 no USB            servidor/app.py  (/ingest)
    sensores               ConvNeXt, geometria, identidade da planta
    fila em disco    --->  banco e series temporais
    interface web

O Pi nao infere. Ele captura, carimba, guarda PRIMEIRO em disco e envia; a
resposta do /ingest (a inferencia feita no notebook) volta e aparece na
interface. Se o notebook estiver fora, a fila cresce e escoa quando ele voltar.

POR QUE BIBLIOTECA PADRAO, E NAO FASTAPI
----------------------------------------
Aqui roda um ThreadingHTTPServer da biblioteca padrao. No Pi, cada dependencia
e um risco: FastAPI puxa pydantic-core, que em armv7 nao tem wheel e compila em
Rust por horas. Uma interface, um MJPEG e meia duzia de rotas JSON nao precisam
de framework. O servidor do notebook continua em FastAPI - la ha espaco.

SEM AUTENTICACAO
----------------
A interface nao tem senha: vive na rede do hotspot do notebook, onde so o que
voce ligou esta. Nao exponha a porta 8080 para fora dela.
"""
import argparse
import json
import mimetypes
import os
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import captura                                       # noqa: E402
from realsense import CameraRealSense                # noqa: E402
from sensores import Painel                          # noqa: E402

INICIO = time.time()


class Estacao:
    """O estado compartilhado entre as threads: camera, sensores, fila e o
    ultimo contato com o servidor."""

    def __init__(self, cfg, base, simular):
        self.cfg, self.base, self.simular = cfg, base, simular
        c = cfg.get("realsense", {})
        self.camera = CameraRealSense(c.get("largura", 640), c.get("altura", 480),
                                      c.get("fps", 15), simular,
                                      c.get("prof_min_m", 0.15), c.get("prof_max_m", 2.0),
                                      c.get("prof_largura"), c.get("prof_altura"))
        entradas = cfg.get("sensores", [])
        if simular:
            entradas = [s for e in entradas for s in _simulacao_de(e)]
        self.painel = Painel(entradas, self.camera)
        self.intervalo = int(cfg.get("intervalo_s", 900))
        self.proxima = time.time() + 10
        self.ultimas = {}
        self.servidor, self.saude_srv, self.visto_srv = None, None, None
        self.trava_captura = threading.Lock()
        self.log = []
        self.banco = os.path.join(base, "historico.db")
        with sqlite3.connect(self.banco) as db:
            db.execute("CREATE TABLE IF NOT EXISTS leitura "
                       "(instante REAL, canal TEXT, valor REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS ix_leitura ON leitura (canal, instante)")

    def registrar(self, msg):
        linha = "%s  %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        print(linha, flush=True)
        self.log = (self.log + [linha])[-60:]

    # ------------------------------------------------------------- lacos
    def laco_sensores(self):
        """Amostra a cada `amostragem_s` e grava no historico local. O historico
        do Pi e para a interface e para diagnostico; o dado que vale para o
        modelo vai no .json de cada captura."""
        passo = int(self.cfg.get("amostragem_s", 15))
        guardar_dias = int(self.cfg.get("historico_dias", 14))
        while True:
            leituras = self.painel.ler()
            self.ultimas = {"instante": time.time(), "valores": leituras}
            agora = time.time()
            with sqlite3.connect(self.banco) as db:
                db.executemany("INSERT INTO leitura VALUES (?,?,?)",
                               [(agora, k, v) for k, v in leituras.items() if v is not None])
                if int(agora) % 3600 < passo:
                    db.execute("DELETE FROM leitura WHERE instante < ?",
                               (agora - guardar_dias * 86400,))
            time.sleep(passo)

    def laco_servidor(self):
        """Descobre o notebook e escoa a fila. Independente da captura: a fila
        acumulada numa queda de rede sai assim que ele volta, sem esperar o
        proximo ciclo de 15 minutos."""
        while True:
            url, saude = captura.resolver_servidor(self.cfg, timeout=2, preferido=self.servidor)
            if url:
                if self.servidor != url:
                    self.registrar("servidor encontrado: %s" % url)
                self.servidor, self.saude_srv = url, saude
                self.visto_srv = time.time()
                if captura.pendentes(self.base):
                    with self.trava_captura:
                        ok, falhou = captura.enviar_fila(self.cfg, self.base, url)
                    if ok or falhou:
                        self.registrar("fila: %d enviados, %d falharam" % (ok, falhou))
                captura.podar(self.base, self.cfg.get("manter_enviados", 2000))
            else:
                if self.servidor:
                    self.registrar("servidor inalcancavel; capturas ficam na fila")
                self.servidor, self.saude_srv = None, None
            time.sleep(10)

    def laco_captura(self):
        while True:
            if time.time() >= self.proxima:
                self.proxima = time.time() + self.intervalo
                try:
                    # com servidor a vista, sobe na hora: a 10 s por ciclo, esperar
                    # o laco de envio dobraria a latencia da inferencia
                    self.capturar(enviar=bool(self.servidor))
                except Exception as erro:
                    self.registrar("captura falhou: %s" % erro)
            time.sleep(1)

    # ------------------------------------------------------------- captura
    def capturar(self, enviar=True):
        """Uma captura completa: cor + profundidade + leituras, gravadas na fila.
        Com `enviar`, sobe na hora e devolve a resposta do servidor."""
        with self.trava_captura:
            pasta = os.path.join(self.base, captura.FILA)
            os.makedirs(pasta, exist_ok=True)
            # o carimbo tem resolucao de segundo; dois cliques no mesmo segundo
            # sobrescreveriam a captura anterior - espera o segundo virar
            while True:
                agora = datetime.now(timezone.utc)
                carimbo = agora.strftime("%Y%m%dT%H%M%SZ")
                jpg = os.path.join(pasta, "%s_%s.jpg" % (self.cfg["camera"], carimbo))
                if not any(os.path.exists(os.path.join(self.base, d, os.path.basename(jpg)))
                           for d in (captura.FILA, captura.ENVIADOS)):
                    break
                time.sleep(0.25)
            info = self.camera.capturar(jpg)
            if info is None:
                self.registrar("camera nao entregou quadro")
                return {"erro": "camera nao entregou quadro"}
            leituras = self.painel.ler()
            meta = {"no": self.cfg["no"], "camera": self.cfg["camera"],
                    "instante": agora.isoformat(), "imagem": os.path.basename(jpg),
                    "leituras": leituras, "camera_info": info}
            with open(jpg.replace(".jpg", ".json"), "w") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2)
            self.registrar("captura %s (profundidade %.0f%% valida)"
                           % (os.path.basename(jpg), 100 * info["profundidade"]["cobertura"]))
            if not enviar:
                return {"captura": os.path.basename(jpg), "enviado": False}
            if not self.servidor:            # recem-ligado: o laco ainda nao procurou
                self.servidor, self.saude_srv = captura.resolver_servidor(self.cfg, timeout=2)
            if not self.servidor:
                return {"captura": os.path.basename(jpg), "enviado": False,
                        "erro": "servidor inalcancavel - ficou na fila"}
            ok, _ = captura.enviar_fila(self.cfg, self.base, self.servidor)
        nome = os.path.basename(jpg).replace(".jpg", ".json")
        feito = os.path.join(self.base, captura.ENVIADOS, nome)
        if ok and os.path.exists(feito):
            with open(feito) as fh:
                return {"captura": os.path.basename(jpg), "enviado": True,
                        "resposta": json.load(fh).get("resposta")}
        return {"captura": os.path.basename(jpg), "enviado": False,
                "erro": "envio falhou - ficou na fila"}

    # ------------------------------------------------------------- consultas
    def estado(self):
        disco = shutil.disk_usage(self.base)
        mem = _meminfo()
        return {
            "no": self.cfg["no"], "camera_nome": self.cfg["camera"],
            "simulado": self.simular,
            "agora": time.time(), "uptime_s": int(time.time() - INICIO),
            "camera": self.camera.estado(),
            "sensores": {"catalogo": self.painel.catalogo, **self.ultimas},
            "fila": {"pendentes": len(captura.pendentes(self.base)),
                     "intervalo_s": self.intervalo,
                     "proxima_em_s": max(0, int(self.proxima - time.time()))},
            "servidor": {"url": self.servidor, "saude": self.saude_srv,
                         "visto": self.visto_srv,
                         "candidatos": self.cfg["servidor"] if isinstance(self.cfg["servidor"], list)
                         else [self.cfg["servidor"]]},
            "sistema": {"carga": os.getloadavg(), "mem_total_mb": mem.get("MemTotal", 0) // 1024,
                        "mem_livre_mb": mem.get("MemAvailable", 0) // 1024,
                        "disco_livre_gb": round(disco.free / 1e9, 1),
                        "disco_total_gb": round(disco.total / 1e9, 1),
                        "maquina": os.uname().machine},
            "log": self.log[-15:],
        }

    def historico(self, horas):
        """Serie por canal, reduzida a ~300 pontos por media em baldes - o
        navegador nao precisa de 40 mil pontos de uma semana."""
        desde = time.time() - horas * 3600
        balde = max(1, int(horas * 3600 / 300))
        with sqlite3.connect(self.banco) as db:
            linhas = db.execute(
                "SELECT canal, CAST(instante / ? AS INT) * ? AS b, AVG(valor), MIN(valor), MAX(valor) "
                "FROM leitura WHERE instante >= ? GROUP BY canal, b ORDER BY b",
                (balde, balde, desde)).fetchall()
        saida = {}
        for canal, b, media, lo, hi in linhas:
            saida.setdefault(canal, []).append([b, round(media, 2), round(lo, 2), round(hi, 2)])
        return saida

    def capturas(self, n=24):
        itens = []
        for pasta, enviado in ((captura.FILA, False), (captura.ENVIADOS, True)):
            caminho = os.path.join(self.base, pasta)
            if not os.path.isdir(caminho):
                continue
            for nome in os.listdir(caminho):
                if nome.endswith(".json"):
                    itens.append((nome, pasta, enviado))
        itens.sort(reverse=True)
        saida = []
        for nome, pasta, enviado in itens[:n]:
            try:
                with open(os.path.join(self.base, pasta, nome)) as fh:
                    meta = json.load(fh)
            except Exception:
                continue
            saida.append({"imagem": meta.get("imagem"), "pasta": pasta, "enviado": enviado,
                          "instante": meta.get("instante"), "leituras": meta.get("leituras"),
                          "resposta": meta.get("resposta"),
                          "profundidade": (meta.get("camera_info") or {}).get("profundidade"),
                          "tem_profundidade": os.path.exists(os.path.join(
                              self.base, pasta, nome.replace(".json", "_prof.png")))})
        return saida


def _simulacao_de(e):
    """O mesmo painel da configuracao real, com cada sensor de hardware trocado
    por um simulado nos MESMOS canais - a interface simulada fica igual a do Pi."""
    t = e.get("tipo")
    if t in ("cpu", "realsense", "simulado"):
        return [e]
    if t in ("dht11", "dht22"):
        return [{"tipo": "simulado", "canal": e.get("canal_temp", "temp"), "nome": "%s temperatura (simulado)" % t.upper(),
                 "base": 25, "amplitude": 5, "min": e.get("min"), "max": e.get("max")},
                {"tipo": "simulado", "canal": e.get("canal_umid", "umid_ar"), "nome": "%s umidade (simulado)" % t.upper(),
                 "unidade": "%", "base": 65, "amplitude": -15, "ruido": 1.0,
                 "min": e.get("min_umid"), "max": e.get("max_umid")}]
    if t == "mcp3008":
        return [{"tipo": "simulado", "canal": "soil%d" % (c + 1), "unidade": "%", "base": 45, "amplitude": 8}
                for c in e.get("canais", [0, 1, 2])]
    if "canal" in e:
        return [{"tipo": "simulado", "canal": e["canal"], "nome": e.get("nome", e["canal"]) + " (simulado)",
                 "unidade": e.get("unidade", "°C"), "min": e.get("min"), "max": e.get("max")}]
    return []


def _meminfo():
    try:
        with open("/proc/meminfo") as fh:
            return {l.split(":")[0]: int(l.split()[1]) for l in fh}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def fabricar_handler(est: Estacao):
    web = os.path.join(AQUI, "web")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _json(self, obj, codigo=200):
            corpo = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def _arquivo(self, caminho, tipo=None):
            if not os.path.isfile(caminho):
                return self._json({"erro": "nao encontrado"}, 404)
            with open(caminho, "rb") as fh:
                corpo = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", tipo or mimetypes.guess_type(caminho)[0]
                             or "application/octet-stream")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _mjpeg(self, fonte):
            """Um quadro por quadro novo da camera, limitado a `stream_fps`: no
            Pi 3, codificar JPEG a 15 fps come um nucleo inteiro por cliente."""
            teto = 1.0 / float(est.cfg.get("stream_fps", 8))
            largura = est.cfg.get("stream_largura", 640)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=quadro")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            seq = -1
            try:
                while True:
                    t = time.time()
                    seq = est.camera.esperar_quadro(seq)
                    jpg = fonte(qualidade=70, largura_max=largura)
                    if jpg:
                        self.wfile.write(b"--quadro\r\nContent-Type: image/jpeg\r\n"
                                         b"Content-Length: %d\r\n\r\n" % len(jpg))
                        self.wfile.write(jpg + b"\r\n")
                    time.sleep(max(0.0, teto - (time.time() - t)))
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._arquivo(os.path.join(web, "index.html"), "text/html; charset=utf-8")
            if u.path == "/stream/cor.mjpg":
                return self._mjpeg(est.camera.jpeg_cor)
            if u.path == "/stream/profundidade.mjpg":
                return self._mjpeg(est.camera.jpeg_profundidade)
            if u.path == "/quadro/cor.jpg":
                jpg = est.camera.jpeg_cor(qualidade=85)
                return self._corpo(jpg, "image/jpeg")
            if u.path == "/api/estado":
                return self._json(est.estado())
            if u.path == "/api/historico":
                return self._json(est.historico(float(q.get("horas", ["6"])[0])))
            if u.path == "/api/capturas":
                return self._json(est.capturas(int(q.get("n", ["24"])[0])))
            if u.path.startswith("/midia/"):
                # /midia/<fila|enviados>/<arquivo>; nada de '..'
                partes = u.path.split("/")[2:]
                if len(partes) == 2 and partes[0] in (captura.FILA, captura.ENVIADOS) \
                        and "/" not in partes[1] and not partes[1].startswith("."):
                    if partes[1].endswith("_prof.png") and q.get("cor"):
                        return self._prof_colorida(os.path.join(est.base, *partes))
                    return self._arquivo(os.path.join(est.base, *partes))
                return self._json({"erro": "caminho invalido"}, 400)
            return self._json({"erro": "rota desconhecida"}, 404)

        def _corpo(self, dados, tipo):
            if not dados:
                return self._json({"erro": "sem quadro"}, 503)
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(dados)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(dados)

        def _prof_colorida(self, caminho):
            """O PNG de 16 bits nao e visivel num navegador: sai colorido."""
            if not os.path.isfile(caminho):
                return self._json({"erro": "nao encontrado"}, 404)
            import numpy as np
            from PIL import Image
            from realsense import _jpeg
            mm = np.asarray(Image.open(caminho)).astype(np.uint16)
            rgb = est.camera.colorir(mm, escala=0.001)
            return self._corpo(_jpeg(rgb, 80, 640), "image/jpeg")

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            corpo = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if u.path == "/api/capturar":
                return self._json(est.capturar(enviar=True))
            if u.path == "/api/intervalo":
                s = int(corpo.get("intervalo_s", 0))
                if not 5 <= s <= 86400:
                    return self._json({"erro": "intervalo entre 5 s e 24 h"}, 400)
                est.intervalo = s
                est.proxima = time.time() + s
                est.registrar("intervalo de captura: %d s" % s)
                return self._json({"intervalo_s": s})
            return self._json({"erro": "rota desconhecida"}, 404)

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--config", default=os.path.join(AQUI, "estacao.json"))
    ap.add_argument("--simular", action="store_true",
                    help="sem D435 nem sensores: tudo sintetico")
    ap.add_argument("--porta", type=int, default=None)
    args = ap.parse_args()

    with open(args.config) as fh:
        cfg = json.load(fh)
    pasta_cfg = os.path.dirname(os.path.abspath(args.config))
    base = os.path.join(pasta_cfg, cfg["base"]) if cfg.get("base") else pasta_cfg
    if args.simular:
        # Simulacao nunca escreve como a camera real: no servidor, a d435-simulada
        # e cadastrada num local proprio, e imagem sintetica nao entra na serie
        # temporal de planta nenhuma do canteiro. Fila separada pelo mesmo motivo.
        cfg["camera"] = cfg.get("camera_simulada", "d435-simulada")
        base = os.path.join(base, "simulada")
    os.makedirs(base, exist_ok=True)
    est = Estacao(cfg, base, args.simular)
    for alvo in (est.laco_sensores, est.laco_servidor, est.laco_captura):
        threading.Thread(target=alvo, daemon=True, name=alvo.__name__).start()

    porta = args.porta or int(cfg.get("porta", 8080))
    srv = ThreadingHTTPServer(("0.0.0.0", porta), fabricar_handler(est))
    srv.daemon_threads = True
    est.registrar("no %s | camera %s (%s) | interface em :%d"
                  % (cfg["no"], cfg["camera"], est.camera.modo, porta))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        est.camera.parar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
