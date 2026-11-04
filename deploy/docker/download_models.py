#!/usr/bin/env python3
"""Descarga de modelos staging de textFlow — catálogo elegido por el usuario.

Filosofía: sólo se descargan modelos de fichero local. Los modelos que el
stack resuelve por endpoint (embeddings, LLM de inferencia, vision/OCR) no
se descargan aquí: el usuario los provee vía variables de entorno.

Uso (host, sólo necesita docker + python3 stdlib):
    python deploy/docker/download_models.py            # menú interactivo
    python deploy/docker/download_models.py --list     # catálogo
    python deploy/docker/download_models.py --models gliner,whisper
    python deploy/docker/download_models.py --all

Las descargas HF se ejecutan dentro de un contenedor python:3.10-slim
(este script se re-invoca a sí mismo con --only), para no exigir deps en
el host. Docling se extrae de su imagen vía docker en el host.

Modo interno (--only job1,job2): descarga HF dos modelos dentro del
contenedor (requiere huggingface_hub). No lance a mano salvo para depurar.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STAGING_DIR = PROJECT_ROOT / "models"
HF_CACHE_DIR = STAGING_DIR / "huggingface_cache"

DOWNLOAD_IMAGE = "python:3.10-slim"
HF_HUB_PIN = "huggingface_hub==2.1.1"

# ---------------------------- catálogo --------------------------------------

# Sólo modelos de fichero local. Los endpoint-resolved viven en
# ENDPOINT_RESOLVED (documentación, no descargables).
CATALOG = {
    "gliner": "Entidades GLiNER — pesos urchade/gliner_small-v2.1 (~583MB) y "
    "backbone deberta-v3-small (~274MB); ifs planos montados desde assets vendidos",
    "whisper": "Audio faster-whisper-large-v2 — Systran/faster-whisper-large-v2 (~3GB)",
    "docling": "Artefactos del docling-serve ~900MB (vía docker desde imagen)",
}

# Documentación de modelos que NO se descargan por quedar fuera de la
# selección: cada backend establece su propio modelo por env vars.
ENDPOINT_RESOLVED = {
    "embeddings": "el modelo del backend abierto compatible: EMBEDDINGS_MODEL "
    "(dashboard, dimensions vía EMBEDDINGS_DIMENSION)",
    "LLM de inferencia": "el modelo del backend que definas en INFERENCE_MODEL",
    "vision/OCR": "el modelo del backend VISION_LLM/OCR (minicpm-v4.5, etc.)",
}

# dependencias automáticas: seleccionar X arrastra estas claves
MODEL_DEPENDENCIES = {
    "gliner": {"gliner_deberta_tokenizer"},
}

# trabajos HF ejecutados dentro del contenedor (deben declarar su repo HF)
HF_JOBS = {
    "gliner_deberta_tokenizer": {
        "repo": "microsoft/deberta-v3-small",
        "required": ["config.json", "spm.model", "tokenizer_config.json", "pytorch_model.bin"],
    },
    "gliner": {
        "repo": "urchade/gliner_small-v2.1",
        "required": ["gliner_config.json", "pytorch_model.bin"],
    },
    "whisper": {
        "repo": "Systran/faster-whisper-large-v2",
        "required": ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"],
    },
}

# ------------------------ selección ----------------------------------------


def expand_selection(tokens: list[str]) -> list[str]:
    """Expandir tokens de selección a lista ordenada de trabajo usable.

    Riesgo: modelado 'all' = catálogo completo (+ dependencias). Los modelos
    seleccionados inválidos abortan listando opciones válidas.
    """
    valid = list(CATALOG) + ["all"]
    if not tokens:
        sys.exit(
            "Selección vacía. Opciones válidas: "
            + ", ".join(valid)
            + " (o ejecuta sin argumentos para el menú interactivo)"
        )
    jobs: list[str] = []
    for token in tokens:
        for part in token.split(","):
            part = part.strip().lower()
            if not part:
                continue
            if part == "all":
                for name in CATALOG:
                    if name not in jobs:
                        jobs.append(name)
                    for dep in MODEL_DEPENDENCIES.get(name, set()):
                        if dep not in jobs:
                            jobs.append(dep)
                continue
            if part not in CATALOG:
                sys.exit(
                    f"Modelo desconocido: '{part}'. Opciones válidas: "
                    + ", ".join(valid)
                )
            if part not in jobs:
                jobs.append(part)
            for dep in MODEL_DEPENDENCIES.get(part, set()):
                if dep not in jobs:
                    jobs.append(dep)
    return jobs


def interactive_selection() -> list[str]:
    """Menú numerado: el usuario elige los modelos que quiera."""
    names = list(CATALOG)
    print("Modelos staging disponibles (elige los que necesites):\n")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name} — {CATALOG[name]}")
    print("\nNo se descargan los modelos resueltos por endpoint (los provee")
    print("tu backend vía env vars):")
    for what, doc in ENDPOINT_RESOLVED.items():
        print(f"  · {what}: {doc}")
    raw = input("\nNúmeros separados por coma/espacio, o 'all': ").strip()
    if raw.lower() in ("a", "all", "todo"):
        return ["all"]
    tokens: list[str] = []
    for chunk in raw.replace(",", " ").split():
        if not chunk.isdigit() or not (1 <= int(chunk) <= len(names)):
            sys.exit(f"Opción fuera de rango: '{chunk}'")
        tokens.append(names[int(chunk) - 1])
    return tokens


# ----------------------- ejecución HF (contenedor) --------------------------


def hf_download(job: str, repo: str, required: list[str]) -> bool:
    """Descarga idempotente de un repo HF; integridad vs su set mínimo."""
    from huggingface_hub import snapshot_download

    repo_dir = HF_CACHE_DIR / "hub" / f"models--{repo.replace('/', '--')}"
    snapshots_dir = repo_dir / "snapshots"

    def snapshot_complete(dir_: Path) -> bool:
        return dir_.is_dir() and all((dir_ / f).exists() for f in required)

    if snapshots_dir.is_dir():
        for snap in sorted(snapshots_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if snapshot_complete(snap):
                print(f"✓ {repo}: snapshot completo ya en staging ({snap.name})")
                return True

    try:
        snap = Path(snapshot_download(repo, cache_dir=HF_CACHE_DIR))
    except Exception as exc:
        print(f"✗ {repo}: descarga falló: {exc}")
        return False

    missing = [f for f in required if not (snap / f).exists()]
    if missing:
        print(f"✗ {repo}: snapshot incompleto, faltan: {', '.join(missing)}")
        return False
    print(f"✓ {repo}: descargado ({snap.name})")
    return True


def run_hf_jobs(jobs: list[str]) -> bool:
    """Descarga HF dentro del contenedor (modo interno --only)."""
    results = [hf_download(name, HF_JOBS[name]["repo"], HF_JOBS[name]["required"]) for name in jobs]
    return all(results)


def dispatch_hf_in_container(jobs: list[str]) -> bool:
    """Reinvoca este script dentro de python:3.10-slim con deps pineadas."""
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{PROJECT_ROOT}:/work",
        "-w", "/work",
        "-e", f"HF_HOME={HF_CACHE_DIR}",
        DOWNLOAD_IMAGE,
        "sh", "-c",
        f"pip install --quiet --no-cache-dir {HF_HUB_PIN} && "
        f"python deploy/docker/download_models.py --only {','.join(jobs)}",
    ]
    return subprocess.call(cmd) == 0


# --------------------------- orquestador -----------------------------------


def print_catalog() -> None:
    print("Catálogo descargable (staging en models/):")
    for name, doc in CATALOG.items():
        print(f"  · {name} — {doc}")
    print("\nExcluidos del catálogo (se resuelven por endpoint, tu backend los provee):")
    for what, doc in ENDPOINT_RESOLVED.items():
        print(f"  · {what}: {doc}")


def run_docling() -> bool:
    host_script = Path(__file__).resolve().parent.parent / "scripts" / "extract_docling.sh"
    if not host_script.is_file():
        print(f"✗ docling: script ausente: {host_script}")
        return False
    return subprocess.call(["bash", str(host_script)]) == 0


def main(argv: list[str]) -> int:
    if "--list" in argv:
        print_catalog()
        return 0

    tokens: list[str] = []
    if "--models" in argv:
        try:
            tokens = argv[argv.index("--models") + 1].split(" ")
        except IndexError:
            sys.exit("--models requiere una lista: --models gliner,whisper")
    elif "--all" in argv or "-A" in argv:
        tokens = ["all"]
    elif "--only" in argv:
        # modo interno dentro del contenedor: trabajos HF ya expandidos
        names = argv[argv.index("--only") + 1].split(",")
        unknown = [n for n in names if n not in HF_JOBS]
        if unknown:
            sys.exit(f"--only acepta sólo trabajos HF; desconocidos: {unknown}")
        return 0 if run_hf_jobs(names) else 1
    else:
        tokens = interactive_selection()

    jobs = expand_selection(tokens)
    ok = True

    hf_selected = [j for j in jobs if j in HF_JOBS]
    if hf_selected:
        print(f"Descargando HF en contenedor ({DOWNLOAD_IMAGE}): {', '.join(hf_selected)}")
        ok &= dispatch_hf_in_container(hf_selected)

    if "docling" in jobs:
        ok &= run_docling()

    print("\nResumen:", "✓ todo preparado" if ok else "✗ hubo fallos — revisa la salida")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
