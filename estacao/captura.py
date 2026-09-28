#!/usr/bin/env python3
"""
No de captura do GAIA, para Raspberry Pi. Foto + leituras -> servidor.

    python3 captura.py --config no.json
    python3 captura.py --config no.json --uma-vez     # um ciclo e sai
    python3 captura.py --config no.json --simular     # sem hardware, para testar

O QUE ESTE PROGRAMA NAO FAZ
---------------------------
Nao roda modelo. Nenhuma rede neural embarca: o Pi captura, carimba e envia; a
inferencia inteira acontece no servidor. Isso e decisao de projeto e esta no
artigo - a comparacao entre variantes do modelo e de custo de SERVICO, nao de
viabilidade de embarque.

E bom que seja assim, porque nao caberia. Um Raspberry Pi 2 Model B tem
**1 GB de RAM** (nao existe versao de 2 GB dele - os de 2 GB sao Pi 4 e Pi 5),
CPU ARMv7 de 900 MHz sem aceleracao util para convolucao, e nenhum WiFi
embutido: precisa de dongle USB. O ConvNeXt-Base sozinho pede ~750 MB so de
ativacao a 384 px. Capturar e enviar, ele faz de sobra.

O SENSOR DE SOLO PRECISA DE UM ADC
----------------------------------
Esta e a pegadinha que custa uma tarde. O Raspberry Pi **nao tem entrada
analogica** - ao contrario do ESP8266, que tem uma. O sensor de umidade de solo
resistivo ou capacitivo entrega tensao, e no Pi ela precisa passar por um
conversor externo:

    MCP3008  (SPI, 10 bits, 8 canais)  - barato, mais que suficiente aqui
    ADS1115  (I2C, 16 bits, 4 canais)  - melhor resolucao, ganho programavel

O codigo abaixo fala com o MCP3008 por SPI. Trocar para ADS1115 e trocar a
classe `LeitorSolo`.

Temperatura e umidade do ar saem de um DHT22 (ou DHT11) num GPIO digital - esse
nao precisa de ADC.

FALHA DE REDE NAO PODE PERDER DADO
----------------------------------
A horta fica onde o WiFi e ruim, e o dado longitudinal e a unica coisa deste
projeto que nao se refaz: perder uma semana de captura custa uma semana de
calendario, nao de GPU. Por isso todo ciclo grava PRIMEIRO em disco, e o envio
e um segundo passo que pode falhar e ser retomado. A fila e um diretorio.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

FILA = "fila"
ENVIADOS = "enviados"


# ---------------------------------------------------------------------------
# hardware, cada um com um substituto de simulacao
# ---------------------------------------------------------------------------
class Camera:
    """picamera2 quando existe; senao libcamera-still; senao simulacao."""

    def __init__(self, largura=1640, altura=1232, simular=False):
        self.largura, self.altura, self.modo = largura, altura, "simulado"
        self._cam = None
        if simular:
            return
        try:
            from picamera2 import Picamera2
            self._cam = Picamera2()
            cfg = self._cam.create_still_configuration(
                main={"size": (largura, altura)})
            self._cam.configure(cfg)
            self._cam.start()
            time.sleep(2)                     # o sensor precisa expor e balancear
            self.modo = "picamera2"
        except Exception as erro:
            print("picamera2 indisponivel (%s); tentando libcamera-still" % erro)
            self.modo = "libcamera" if os.system("which libcamera-still >/dev/null 2>&1") == 0 \
                else "simulado"

    def capturar(self, destino: str) -> bool:
        if self.modo == "picamera2":
            self._cam.capture_file(destino)
            return True
        if self.modo == "libcamera":
            cmd = ("libcamera-still -n -t 1500 --width %d --height %d -o %s "
                   ">/dev/null 2>&1" % (self.largura, self.altura, destino))
            return os.system(cmd) == 0
        # simulado: uma imagem sintetica, so para exercitar o caminho todo
        try:
            from PIL import Image
            Image.new("RGB", (self.largura, self.altura), (90, 120, 70)).save(destino)
            return True
        except Exception:
            with open(destino, "wb") as fh:
                fh.write(b"")
            return True


class LeitorDHT:
    """DHT22/DHT11 num GPIO digital: temperatura e umidade do ar."""

    def __init__(self, pino=4, modelo="DHT22", simular=False):
        self.pino, self.modelo, self._dht = pino, modelo, None
        if simular:
            return
        try:
            import adafruit_dht
            import board
            classe = adafruit_dht.DHT22 if modelo == "DHT22" else adafruit_dht.DHT11
            try:
                self._dht = classe(getattr(board, "D%d" % pino))
            except Exception:
                # pulseio precisa de libgpiod + sysv_ipc; sem eles, bit-bang pelo
                # RPi.GPIO - menos preciso no tempo, mas o DHT tolera e repete
                self._dht = classe(getattr(board, "D%d" % pino), use_pulseio=False)
        except Exception as erro:
            print("DHT indisponivel (%s); leituras entram como ausentes" % erro)

    def ler(self):
        """Devolve (temperatura_C, umidade_ar_%). None onde falhou.

        O DHT falha com frequencia e isso e normal - checksum ruim, timing
        perdido. Falha vira None, e None vira flag 0 no vetor do modelo. Nunca
        se inventa um valor: o par (valor, flag) existe exatamente para que
        'sem leitura' seja representavel."""
        if self._dht is None:
            return None, None
        for _ in range(3):
            try:
                return self._dht.temperature, self._dht.humidity
            except RuntimeError:
                time.sleep(2)
            except Exception:
                break
        return None, None


class LeitorSolo:
    """Umidade de solo via MCP3008 no SPI. O Pi nao tem entrada analogica."""

    def __init__(self, canais=(0, 1, 2), seco=None, molhado=None, simular=False):
        self.canais = list(canais)
        # Calibracao POR SENSOR, obrigatoria: sonda capacitiva no ar e sonda na
        # agua dao valores diferentes de uma para outra, e a leitura crua nao
        # significa nada sem esses dois pontos. Sem calibracao, devolve None -
        # e melhor ausente do que um percentual inventado.
        self.seco = seco or {}
        self.molhado = molhado or {}
        self._spi = None
        if simular:
            return
        try:
            import spidev
            self._spi = spidev.SpiDev()
            self._spi.open(0, 0)
            self._spi.max_speed_hz = 1350000
        except Exception as erro:
            print("SPI/MCP3008 indisponivel (%s); solo entra como ausente" % erro)

    def _cru(self, canal: int):
        if self._spi is None:
            return None
        r = self._spi.xfer2([1, (8 + canal) << 4, 0])
        return ((r[1] & 3) << 8) + r[2]                 # 0..1023

    def ler(self):
        """{canal: percentual}. Canal sem calibracao ou sem SPI vira None."""
        saida = {}
        for c in self.canais:
            nome = "soil%d" % (c + 1)
            cru = self._cru(c)
            s, m = self.seco.get(nome), self.molhado.get(nome)
            if cru is None or s is None or m is None or s == m:
                saida[nome] = None
                continue
            pct = 100.0 * (s - cru) / float(s - m)      # seco = leitura alta
            saida[nome] = max(0.0, min(100.0, pct))
        return saida


# ---------------------------------------------------------------------------
# ciclo
# ---------------------------------------------------------------------------
def um_ciclo(cfg, camera, dht, solo, base):
    agora = datetime.now(timezone.utc)
    carimbo = agora.strftime("%Y%m%dT%H%M%SZ")
    pasta = os.path.join(base, FILA)
    os.makedirs(pasta, exist_ok=True)

    jpg = os.path.join(pasta, "%s_%s.jpg" % (cfg["camera"], carimbo))
    if not camera.capturar(jpg):
        print("%s  falha na captura" % carimbo)
        return None

    temp, ur = dht.ler()
    leituras = {"temp": temp, "umid_ar": ur}
    leituras.update(solo.ler())

    meta = {
        "no": cfg["no"],
        "camera": cfg["camera"],
        "instante": agora.isoformat(),
        "imagem": os.path.basename(jpg),
        # Canais crus, sem interpretacao. Quem sabe QUAL sensor e cada canal, e
        # onde ele esta, e o servidor - a posicao mora no banco, nao aqui.
        "leituras": leituras,
    }
    with open(jpg.replace(".jpg", ".json"), "w") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    presentes = {k: v for k, v in leituras.items() if v is not None}
    print("%s  %-22s  %s" % (carimbo, os.path.basename(jpg),
                             presentes or "nenhuma leitura"))
    return jpg


def resolver_servidor(cfg, timeout=3, preferido=None):
    """`servidor` pode ser uma URL ou uma lista delas; vale a primeira que
    responder /saude. A lista existe porque o mesmo no.json roda no Pi de
    verdade (notebook em 10.42.0.1, pelo hotspot) e na VM do QEMU (notebook em
    10.0.2.2, pela rede do usuario) sem ser editado."""
    urls = cfg["servidor"] if isinstance(cfg["servidor"], list) else [cfg["servidor"]]
    if preferido in urls:                 # o que respondeu da ultima vez primeiro
        urls = [preferido] + [u for u in urls if u != preferido]
    try:
        import requests
    except ImportError:
        return None, None
    for url in urls:
        try:
            r = requests.get(url.rstrip("/") + "/saude", timeout=timeout)
            if r.status_code < 300:
                return url.rstrip("/"), r.json()
        except Exception:
            continue
    return None, None


def enviar_fila(cfg, base, servidor=None, limite=None):
    """Sobe tudo que estiver na fila. O que falhar fica para o proximo ciclo.

    A resposta do servidor (a inferencia) e gravada no proprio .json antes de
    ele ir para enviados/ - e dali que a interface do no mostra o resultado."""
    try:
        import requests
    except ImportError:
        print("requests nao instalado; a fila fica em disco")
        return 0, 0

    if servidor is None:
        servidor, _ = resolver_servidor(cfg)
        if servidor is None:
            return 0, len(pendentes(base))
    pasta = os.path.join(base, FILA)
    destino = os.path.join(base, ENVIADOS)
    os.makedirs(destino, exist_ok=True)
    ok = falhou = 0

    for nome in pendentes(base)[:limite]:
        cam_json = os.path.join(pasta, nome)
        cam_jpg = cam_json.replace(".json", ".jpg")
        # profundidade alinhada a cor, 16 bits em milimetros; so existe com a
        # RealSense. O servidor antigo ignora o campo, entao enviar nao quebra.
        cam_prof = cam_json.replace(".json", "_prof.png")
        if not os.path.exists(cam_jpg):
            continue
        try:
            with open(cam_json) as fh:
                meta = json.load(fh)
            arquivos = {"imagem": (os.path.basename(cam_jpg), open(cam_jpg, "rb"),
                                   "image/jpeg")}
            if os.path.exists(cam_prof):
                arquivos["profundidade"] = (os.path.basename(cam_prof),
                                            open(cam_prof, "rb"), "image/png")
            try:
                r = requests.post(
                    servidor + "/ingest", files=arquivos,
                    data={"meta": json.dumps(meta)},
                    timeout=cfg.get("timeout_s", 30),
                )
            finally:
                for _, fh, _ in arquivos.values():
                    fh.close()
            if r.status_code < 300:
                try:
                    meta["resposta"] = r.json()
                except ValueError:
                    meta["resposta"] = {"texto": r.text[:500]}
                meta["enviado_em"] = datetime.now(timezone.utc).isoformat()
                with open(cam_json, "w") as fh:
                    json.dump(meta, fh, ensure_ascii=False, indent=2)
                for arq in (cam_jpg, cam_prof, cam_json):
                    if os.path.exists(arq):
                        os.rename(arq, os.path.join(destino, os.path.basename(arq)))
                ok += 1
            else:
                print("servidor devolveu %s para %s: %s" % (r.status_code, nome, r.text[:200]))
                falhou += 1
        except Exception as erro:
            print("envio falhou (%s): %s" % (type(erro).__name__, erro))
            falhou += 1
            break                     # rede caiu; nao adianta insistir agora
    return ok, falhou


def pendentes(base):
    pasta = os.path.join(base, FILA)
    if not os.path.isdir(pasta):
        return []
    return sorted(n for n in os.listdir(pasta) if n.endswith(".json"))


def podar(base, manter=2000):
    """A fila nao pode encher o cartao. Se o servidor ficar dias fora, os mais
    antigos saem primeiro - dado recente vale mais para trajetoria."""
    pasta = os.path.join(base, ENVIADOS)
    if not os.path.isdir(pasta):
        return
    arquivos = sorted(os.listdir(pasta))
    # manter conta capturas, e cada captura e ate tres arquivos (jpg, png, json)
    for nome in arquivos[:max(0, len(arquivos) - 3 * manter)]:
        try:
            os.remove(os.path.join(pasta, nome))
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--config", default="no.json")
    ap.add_argument("--uma-vez", action="store_true")
    ap.add_argument("--simular", action="store_true",
                    help="sem hardware: exercita captura, fila e envio")
    args = ap.parse_args()

    with open(args.config) as fh:
        cfg = json.load(fh)
    base = cfg.get("base", os.path.dirname(os.path.abspath(args.config)))

    camera = Camera(cfg.get("largura", 1640), cfg.get("altura", 1232), args.simular)
    dht = LeitorDHT(cfg.get("dht_pino", 4), cfg.get("dht_modelo", "DHT22"), args.simular)
    solo = LeitorSolo(cfg.get("solo_canais", [0, 1, 2]),
                      cfg.get("solo_seco"), cfg.get("solo_molhado"), args.simular)

    print("no %s | camera %s (%s) | servidor %s"
          % (cfg["no"], cfg["camera"], camera.modo, cfg["servidor"]))
    intervalo = int(cfg.get("intervalo_s", 900))

    while True:
        um_ciclo(cfg, camera, dht, solo, base)
        ok, falhou = enviar_fila(cfg, base)
        if ok or falhou:
            print("   fila: %d enviados, %d pendentes" % (ok, falhou))
        podar(base)
        if args.uma_vez:
            return 0
        time.sleep(intervalo)


if __name__ == "__main__":
    sys.exit(main())
