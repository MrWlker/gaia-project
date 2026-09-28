#!/usr/bin/env python3
"""
Cadastro de local, camera e sensor no banco do servidor - o /ingest recusa
camera sem extrinseca, porque sem ela nao ha como projetar deteccao no canteiro.

    python3 cadastrar.py local canteiro-1
    python3 cadastrar.py camera d435-frontal --local 1 --y -60 --z 80 --pitch -45 \\
                                --fov 69.4 --largura 640 --altura 480
    python3 cadastrar.py sensor ar-central --local 1 --canal temp --tipo temperature --central
    python3 cadastrar.py listar

Distancias em cm a partir do controlador; angulos em graus (convencao em
gaia/geometria.py). A D435 a 640x480 tem FOV horizontal de cor de ~69,4°;
a estacao mostra o valor exato, lido da intrinseca de fabrica.

Usa o mesmo GAIA_BANCO que o app.py.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sqlalchemy import select                              # noqa: E402
from sqlalchemy.orm import Session                         # noqa: E402
from app import Base, Camera, Local, Sensor, motor         # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = ap.add_subparsers(dest="o", required=True)
    l = sub.add_parser("local"); l.add_argument("nome")
    c = sub.add_parser("camera"); c.add_argument("nome")
    s = sub.add_parser("sensor"); s.add_argument("nome")
    for p in (c, s):
        p.add_argument("--local", type=int, required=True)
        p.add_argument("--x", type=float, default=0.0)
        p.add_argument("--y", type=float, default=0.0)
        p.add_argument("--z", type=float, default=0.0)
    c.add_argument("--yaw", type=float, default=0.0)
    c.add_argument("--pitch", type=float, required=True)
    c.add_argument("--fov", type=float, default=69.4)
    c.add_argument("--largura", type=int, default=640)
    c.add_argument("--altura", type=int, default=480)
    s.add_argument("--canal", required=True, help="o nome que o no manda: temp, soil1, ...")
    s.add_argument("--tipo", required=True, help="temperature | air_humidity | soil_moisture ...")
    s.add_argument("--central", action="store_true")
    s.add_argument("--profundidade", type=int, default=None)
    sub.add_parser("listar")
    a = ap.parse_args()

    Base.metadata.create_all(motor)
    with Session(motor) as db:
        if a.o == "local":
            db.add(Local(nome=a.nome))
        elif a.o == "camera":
            cam = db.scalar(select(Camera).where(Camera.nome == a.nome)) or Camera(nome=a.nome)
            cam.local_id, cam.offset_x, cam.offset_y, cam.offset_z = a.local, a.x, a.y, a.z
            cam.yaw, cam.pitch, cam.fov_h = a.yaw, a.pitch, a.fov
            cam.largura_px, cam.altura_px = a.largura, a.altura
            db.add(cam)
        elif a.o == "sensor":
            db.add(Sensor(nome=a.nome, local_id=a.local, canal=a.canal, tipo=a.tipo,
                          offset_x=a.x, offset_y=a.y, offset_z=a.z, central=a.central,
                          profundidade_cm=a.profundidade))
        db.commit()
        for L in db.scalars(select(Local)):
            print("local  %d  %s" % (L.id, L.nome))
        for k in db.scalars(select(Camera)):
            print("camera %d  %-16s local %s  (%g, %g, %g) cm  yaw %g  pitch %g  fov %g  %dx%d"
                  % (k.id, k.nome, k.local_id, k.offset_x, k.offset_y, k.offset_z,
                     k.yaw, k.pitch, k.fov_h, k.largura_px, k.altura_px))
        for k in db.scalars(select(Sensor)):
            print("sensor %d  %-16s local %s  canal %-8s %s  (%g, %g) cm%s"
                  % (k.id, k.nome, k.local_id, k.canal, k.tipo, k.offset_x, k.offset_y,
                     "  central" if k.central else ""))


if __name__ == "__main__":
    main()
