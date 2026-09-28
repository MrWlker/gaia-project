"""
Intel RealSense D435 no no de captura: cor + profundidade alinhada.

UM PIPELINE SO, DUAS COISAS QUE O USAM
--------------------------------------
A camera nao abre duas vezes. O stream ao vivo da interface e a captura
periodica saem do MESMO pipeline: uma thread le quadros o tempo todo e guarda
o ultimo; o stream codifica esse ultimo em JPEG, e a captura pede o proximo
quadro ALINHADO (profundidade reprojetada para a geometria da cor).

O alinhamento so acontece na captura. `rs.align` a 640x480 custa dezenas de
milissegundos num Cortex-A53 - pago uma vez a cada 15 minutos, tudo bem; pago
a 15 fps, o Pi 3 nao faz mais nada.

O PI 3 SO TEM USB 2.0
---------------------
A D435 e USB 3 e funciona em USB 2 com um subconjunto de modos. Os que valem
aqui, testados como estaveis em USB 2.1:

    cor         640x480  @ 6 / 15 / 30
    profundidade 640x480 @ 6 / 15 / 30   (z16)
    cor         1280x720 @ 6             (so ela, sem profundidade junto)

Por isso o padrao e 640x480 @ 15 nos dois. Na VM do QEMU, com qemu-xhci, a
camera pode subir como USB 3 e aceitar modos maiores - mas o alvo e o Pi.

O QUE A CAPTURA GUARDA
----------------------
    <camera>_<instante>.jpg        cor, qualidade 92
    <camera>_<instante>_prof.png   profundidade em MILIMETROS, 16 bits, alinhada a cor
    no .json                       intrinseca REAL da cor (fx, fy, cx, cy)

A intrinseca de fabrica e o que a geometria.py hoje estima por trena e fov_h:
com ela, a projecao de pixel deixa de depender de medida a mao. E a
profundidade por pixel e a primeira medida metrica da planta que o projeto tem
- altura e volume de dossel, e nao so cor.
"""
import io
import math
import threading
import time

try:
    import numpy as np
except ImportError:                     # sem numpy nao ha nem simulacao util
    np = None

USB2_SEGURO = (640, 480, 15)


def _lut_turbo():
    """Mapa de cores 'turbo' (Google, 2019) em 256 entradas, pela aproximacao
    polinomial publicada - sem OpenCV, que no Pi e mais uma dependencia pesada."""
    t = np.linspace(0.0, 1.0, 256)
    r = 0.13572138 + t * (4.61539260 + t * (-42.66032258 + t * (132.13108234 + t * (-152.94239396 + t * 59.28637943))))
    g = 0.09140261 + t * (2.19418839 + t * (4.84296658 + t * (-14.18503333 + t * (4.27729857 + t * 2.82956604))))
    b = 0.10667330 + t * (12.64194608 + t * (-60.58204836 + t * (110.36276771 + t * (-89.90310912 + t * 27.34824973))))
    lut = np.stack([r, g, b], axis=1)
    return (np.clip(lut, 0, 1) * 255).astype(np.uint8)


class CameraRealSense:
    """Le a D435 numa thread. Sem pyrealsense2 ou sem camera, simula - o
    resto do no (interface, fila, envio) funciona igual e pode ser testado no
    notebook."""

    def __init__(self, largura=640, altura=480, fps=15, simular=False,
                 prof_min_m=0.15, prof_max_m=2.0, prof_largura=None, prof_altura=None):
        self.largura, self.altura, self.fps = largura, altura, fps
        # a profundidade tem teto proprio (1280x720 na D435); a cor vai a
        # 1920x1080. O alinhamento reprojeta uma na outra, entao podem diferir.
        self.prof_largura = prof_largura or min(largura, 1280)
        self.prof_altura = prof_altura or min(altura, 720)
        self.prof_min_m, self.prof_max_m = prof_min_m, prof_max_m
        self.modo = "simulado"
        self.info = {}                      # modelo, serial, firmware, usb
        self.erro = None
        self.fps_medido = 0.0
        self._rs = None
        self._pipe = None
        self._escala = 0.001                # metros por unidade z16
        self._intr = None
        self._cor = None                    # ultimo quadro de cor, HxWx3 uint8
        self._prof = None                   # ultimo de profundidade, HxW uint16 (nao alinhado)
        self._trava = threading.Lock()
        self._novo = threading.Condition(self._trava)
        self._seq = 0
        self._pedido = None                 # captura alinhada pendente
        self._rodando = True
        self._lut = _lut_turbo() if np is not None else None
        if not simular:
            self._abrir()
        threading.Thread(target=self._laco, daemon=True, name="realsense").start()

    # ------------------------------------------------------------------ abrir
    def _abrir(self):
        try:
            import pyrealsense2 as rs
        except ImportError as erro:
            self.erro = "pyrealsense2 nao instalado (%s)" % erro
            print(self.erro + "; camera simulada")
            return
        self._rs = rs
        ctx = rs.context()
        if len(ctx.query_devices()) == 0:
            self.erro = "nenhuma RealSense no USB"
            print(self.erro + "; camera simulada (confira lsusb: 8086:0b07)")
            return
        dev = ctx.query_devices()[0]
        g = lambda k: dev.get_info(k) if dev.supports(k) else None
        self.info = {"modelo": g(rs.camera_info.name),
                     "serial": g(rs.camera_info.serial_number),
                     "firmware": g(rs.camera_info.firmware_version),
                     "usb": g(rs.camera_info.usb_type_descriptor)}
        usb = self.info.get("usb") or ""
        if usb.startswith("2") and (self.largura, self.altura) != USB2_SEGURO[:2]:
            print("USB %s: forcando %dx%d@%d, o unico modo cor+profundidade "
                  "estavel em USB 2" % ((usb,) + USB2_SEGURO))
            self.largura, self.altura, self.fps = USB2_SEGURO
            self.prof_largura, self.prof_altura = USB2_SEGURO[:2]
        cfg = rs.config()
        cfg.enable_device(self.info["serial"])
        cfg.enable_stream(rs.stream.color, self.largura, self.altura, rs.format.rgb8, self.fps)
        cfg.enable_stream(rs.stream.depth, self.prof_largura, self.prof_altura, rs.format.z16, self.fps)
        try:
            self._pipe = rs.pipeline()
            perfil = self._pipe.start(cfg)
        except Exception as erro:
            self.erro = "pipeline nao abriu: %s" % erro
            print(self.erro + "; camera simulada")
            self._pipe = None
            return
        sensor_prof = perfil.get_device().first_depth_sensor()
        self._escala = sensor_prof.get_depth_scale()
        self._sensor_prof = sensor_prof
        vcor = perfil.get_stream(rs.stream.color).as_video_stream_profile()
        i = vcor.get_intrinsics()
        self._intr = {"fx": i.fx, "fy": i.fy, "cx": i.ppx, "cy": i.ppy,
                      "largura": i.width, "altura": i.height,
                      "fov_h": math.degrees(2 * math.atan(i.width / (2 * i.fx))),
                      "modelo_distorcao": str(i.model), "coefs": list(i.coeffs)}
        self._align = rs.align(rs.stream.color)
        self.modo = "realsense"
        print("RealSense %s (serial %s, fw %s, USB %s) cor %dx%d, profundidade %dx%d @%d"
              % (self.info["modelo"], self.info["serial"], self.info["firmware"],
                 usb, self.largura, self.altura, self.prof_largura, self.prof_altura, self.fps))
        # descarta os primeiros quadros: auto-exposicao ainda convergindo
        for _ in range(self.fps):
            try:
                self._pipe.wait_for_frames(2000)
            except Exception:
                break

    # ------------------------------------------------------------------ laco
    def _laco(self):
        t0, n = time.time(), 0
        while self._rodando:
            try:
                if self._pipe is not None:
                    self._ler_real()
                else:
                    self._ler_simulado()
            except Exception as erro:
                # USB solto, energia insuficiente no hub - acontece no Pi.
                self.erro = "leitura falhou: %s" % erro
                time.sleep(1)
                continue
            n += 1
            if time.time() - t0 >= 2:
                self.fps_medido, t0, n = n / (time.time() - t0), time.time(), 0

    def _ler_real(self):
        quadros = self._pipe.wait_for_frames(5000)
        pedido = self._pedido
        if pedido is not None:
            alinhados = self._align.process(quadros)
            c, d = alinhados.get_color_frame(), alinhados.get_depth_frame()
            if c and d:
                pedido["cor"] = np.asanyarray(c.get_data()).copy()
                pedido["prof"] = np.asanyarray(d.get_data()).copy()
                self._pedido = None
                pedido["pronto"].set()
        c, d = quadros.get_color_frame(), quadros.get_depth_frame()
        with self._novo:
            if c:
                self._cor = np.asanyarray(c.get_data()).copy()
            if d:
                self._prof = np.asanyarray(d.get_data()).copy()
            self._seq += 1
            self._novo.notify_all()

    def _ler_simulado(self):
        time.sleep(1.0 / self.fps)
        if np is None:
            return
        t = time.time()
        h, w = self.altura, self.largura
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        # canteiro verde com tres "plantas" que respiram - so para o caminho
        cor = np.empty((h, w, 3), np.uint8)
        cor[..., 0] = 70 + 30 * (yy / h)
        cor[..., 1] = 95 + 40 * (yy / h)
        cor[..., 2] = 50
        prof = (1200 - 500 * (yy / h)).astype(np.float32)   # chao inclinado, mm
        for k, (px, py) in enumerate(((0.25, 0.6), (0.5, 0.55), (0.75, 0.62))):
            raio = (0.09 + 0.01 * math.sin(t / 3 + k)) * w
            m = (xx - px * w) ** 2 + (yy - py * h) ** 2 < raio ** 2
            cor[m] = (40 + 20 * k, 150 + 10 * k, 60)
            prof[m] -= 180
        d = prof.astype(np.uint16)
        pedido = self._pedido
        if pedido is not None:
            pedido["cor"], pedido["prof"] = cor, d
            self._pedido = None
            pedido["pronto"].set()
        with self._novo:
            self._cor, self._prof = cor, d
            self._seq += 1
            self._novo.notify_all()

    # ------------------------------------------------------------------ api
    def esperar_quadro(self, seq_anterior, timeout=2.0):
        """Bloqueia ate chegar um quadro depois de `seq_anterior`. E o que deixa
        o MJPEG andar no ritmo da camera sem girar em falso."""
        with self._novo:
            self._novo.wait_for(lambda: self._seq != seq_anterior, timeout)
            return self._seq

    def jpeg_cor(self, qualidade=75, largura_max=None):
        with self._trava:
            cor = self._cor
        if cor is None:
            return None
        return _jpeg(cor, qualidade, largura_max)

    def jpeg_profundidade(self, qualidade=75, largura_max=None):
        with self._trava:
            prof = self._prof
        if prof is None:
            return None
        return _jpeg(self.colorir(prof), qualidade, largura_max)

    def colorir(self, prof, escala=None):
        """z16 -> RGB. Perto = quente, longe = frio, sem leitura = preto.
        `escala` em metros por unidade; o PNG gravado e sempre milimetro."""
        m = prof.astype(np.float32) * (escala or self._escala)
        n = (m - self.prof_min_m) / (self.prof_max_m - self.prof_min_m)
        idx = (np.clip(1.0 - n, 0, 1) * 255).astype(np.uint8)
        rgb = self._lut[idx]
        rgb[prof == 0] = 0
        return rgb

    def distancia_centro_m(self):
        """Mediana da profundidade numa janela central - para a interface dizer
        a que distancia o canteiro esta, que e o que se ajusta na montagem."""
        with self._trava:
            prof = self._prof
        if prof is None:
            return None
        h, w = prof.shape
        janela = prof[h // 2 - 10:h // 2 + 10, w // 2 - 10:w // 2 + 10]
        validos = janela[janela > 0]
        return float(np.median(validos)) * self._escala if validos.size else None

    def temperatura_asic(self):
        """A D435 expoe a temperatura do ASIC e do projetor. Nao e ambiente,
        mas acusa camera cozinhando ao sol - que derruba a qualidade da
        profundidade antes de derrubar a camera."""
        if self._pipe is None:
            return None, None
        rs = self._rs
        saida = []
        for opc in (rs.option.asic_temperature, rs.option.projector_temperature):
            try:
                saida.append(float(self._sensor_prof.get_option(opc))
                             if self._sensor_prof.supports(opc) else None)
            except Exception:
                saida.append(None)
        return tuple(saida)

    def capturar(self, destino_jpg, timeout=5.0):
        """Grava cor (.jpg) e profundidade alinhada (_prof.png, mm, 16 bits).
        Devolve o bloco de metadados da camera, ou None se falhou."""
        if np is None:
            return None
        pedido = {"pronto": threading.Event()}
        self._pedido = pedido
        if not pedido["pronto"].wait(timeout):
            self._pedido = None
            return None
        from PIL import Image
        Image.fromarray(pedido["cor"]).save(destino_jpg, quality=92)
        prof = pedido["prof"]
        mm = prof if abs(self._escala - 0.001) < 1e-9 else \
            np.clip(prof.astype(np.float32) * self._escala * 1000, 0, 65535).astype(np.uint16)
        Image.fromarray(mm.astype(np.uint16)).save(
            destino_jpg.replace(".jpg", "_prof.png"))
        validos = mm[mm > 0]
        return {
            "modo": self.modo,
            "dispositivo": self.info or None,
            "largura": int(prof.shape[1]), "altura": int(prof.shape[0]),
            "intrinseca_cor": self._intr,
            "profundidade": {"unidade": "mm", "alinhada_a": "cor",
                             "cobertura": round(float(validos.size) / mm.size, 3),
                             "mediana_mm": float(np.median(validos)) if validos.size else None},
        }

    def estado(self):
        asic, proj = self.temperatura_asic()
        return {"modo": self.modo, "erro": self.erro, "info": self.info,
                "largura": self.largura, "altura": self.altura, "fps": self.fps,
                "prof_largura": self.prof_largura, "prof_altura": self.prof_altura,
                "fps_medido": round(self.fps_medido, 1),
                "distancia_centro_m": self.distancia_centro_m(),
                "temp_asic": asic, "temp_projetor": proj,
                "intrinseca": self._intr}

    def parar(self):
        self._rodando = False
        if self._pipe is not None:
            try:
                self._pipe.stop()
            except Exception:
                pass


def _jpeg(rgb, qualidade, largura_max):
    from PIL import Image
    img = Image.fromarray(rgb)
    if largura_max and img.width > largura_max:
        img = img.resize((largura_max, int(img.height * largura_max / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=qualidade)
    return buf.getvalue()
