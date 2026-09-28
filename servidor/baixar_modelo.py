#!/usr/bin/env python3
"""
Baixa o modelo pre-treinado do Hugging Face para ../modelo.

    python3 baixar_modelo.py                          # repositorio padrao
    python3 baixar_modelo.py --repo usuario/gaia-convnext
    GAIA_HF_REPO=usuario/gaia-convnext python3 baixar_modelo.py

So o modelo e publicado (pesos + espaco de classes + configuracao). O codigo
de treino acompanha o artigo e nao e necessario para inferir.
"""
import argparse
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PADRAO = os.environ.get("GAIA_HF_REPO", "MrWlker/gaia-convnext")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--repo", default=PADRAO, help="repositorio do modelo no Hugging Face")
    ap.add_argument("--destino", default=os.path.join(RAIZ, "modelo"))
    ap.add_argument("--revisao", default=None, help="tag ou commit, para fixar a versao")
    a = ap.parse_args()
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        sys.exit("falta huggingface_hub:  pip install huggingface_hub")
    caminho = snapshot_download(repo_id=a.repo, revision=a.revisao, local_dir=a.destino,
                                allow_patterns=["*.pt", "*.json", "*.md"])
    print("modelo em %s" % caminho)
    for f in sorted(os.listdir(caminho)):
        if not f.startswith("."):
            print("  %s" % f)


if __name__ == "__main__":
    main()
