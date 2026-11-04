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
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STAGING_DIR = PROJECT_ROOT / "models"
# staging alternativo (tests, segunda copia): MODELS_STAGING_DIR override
if os.environ.get("MODELS_STAGING_DIR"):
    STAGING_DIR = Path(os.environ["MODELS_STAGING_DIR"])
HF_CACHE_DIR = STAGING_DIR / "huggingface_cache"
ASSETS_DIR = PROJECT_ROOT / "deploy" / "models-assets"

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
        snap = Path(snapshot_download(repo, cache_dir=HF_CACHE_DIR / "hub"))
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
    """Reinvoca este script dentro de python:3.10-slim con deps pineadas.

    Monta     el staging ACTIVO (respetando MODELS_STAGING_DIR) para que las
    descargas y los checks de snapshot recaigan en el staging correcto.
    """
    # crea el punto de montaje como usuario: docker lo créería root:root y
    # el ensamblador del host no podría escribir después
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "docker", "run", "--rm",
        # NO root: los ficheros descargados quedarían 0600 root:root y el
        # ensamblador del host no podría leerlos (bloqueante de 1ª install)
        "-u", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp",       # pip --user escrituras de site-packages
        "-v", f"{PROJECT_ROOT}:/work",
        "-v", f"{STAGING_DIR}:/models-staging",
        "-w", "/work",
        # HF_HOME define el nivel hub/ del cache (X/hub/models--*) — sin él
        # snapshot_download escribe models--* directamente en cache_dir
        "-e", "HF_HOME=/models-staging/huggingface_cache",
        "-e", "MODELS_STAGING_DIR=/models-staging",
        DOWNLOAD_IMAGE,
        "sh", "-c",
        f"pip install --user --quiet --no-cache-dir {HF_HUB_PIN} && "
        f"python deploy/docker/download_models.py --only {','.join(jobs)}",
    ]
    return subprocess.call(cmd) == 0


# --------------------------- ensamblado staging ----------------------------


def _latest_complete_snapshot(repo: str, required: list[str]) -> Path:
    """Snapshot más reciente que contenga todos los ficheros requeridos."""
    snapshots = HF_CACHE_DIR / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots"
    if not snapshots.is_dir():
        sys.exit(f"Snapshot de {repo} ausente en staging. Ejecuta make setup-models.")
    for snap in sorted(snapshots.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if all((snap / f).is_file() for f in required):
            return snap
    sys.exit(
        f"Snapshot de {repo} incompleto (faltan {', '.join(required)}). "
        "Ejecuta make setup-models para re-descargarlo."
    )


def _copy(src: Path, dst: Path, follow: bool = True) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()  # blobs HF quedan r--r--r--: sobreescribir fallaría
    shutil.copy(_resolve(src) if follow else src, dst)


def _resolve(p: Path) -> Path:
    """Resuelve symlinks dentro del arbol HF cache (CAS: blobs/<shard>)."""
    return Path(os.path.realpath(p))


def assemble_gliner() -> None:
    """models/gliner-small-v2.1/: assets vendidos + pesos del snapshot urchade."""
    snap = _latest_complete_snapshot("urchade/gliner_small-v2.1", ["pytorch_model.bin"])
    flat = STAGING_DIR / "gliner-small-v2.1"
    flat.mkdir(parents=True, exist_ok=True)
    for f in (ASSETS_DIR / "gliner-small-v2.1").iterdir():
        _copy(f, flat / f.name)
    _copy(snap / "pytorch_model.bin", flat / "pytorch_model.bin")
    print(f"✓ gliner-small-v2.1/: dir plano listo ({flat})")


def assemble_deberta() -> None:
    """models/deberta-v3-small/: ficheros del snapshot + special_tokens_map vendido."""
    snap = _latest_complete_snapshot(
        "microsoft/deberta-v3-small",
        ["config.json", "spm.model", "tokenizer_config.json", "pytorch_model.bin"],
    )
    flat = STAGING_DIR / "deberta-v3-small"
    flat.mkdir(parents=True, exist_ok=True)
    for f in ("config.json", "spm.model", "tokenizer_config.json", "pytorch_model.bin"):
        _copy(snap / f, flat / f)
    _copy(ASSETS_DIR / "deberta-v3-small" / "special_tokens_map.json",
          flat / "special_tokens_map.json")
    print(f"✓ deberta-v3-small/: dir plano listo ({flat})")


def _fix_dangling_symlinks(dst: Path, source_root: Path) -> None:
    """Trae del arbol fuente los ficheros reales de symlinks colgados."""
    for _ in range(4):
        pending = 0
        for root, _dirs, files in os.walk(dst):
            for name in files:
                link = Path(root) / name
                if not link.is_symlink():
                    continue
                target = link.parent / os.readlink(link)
                if target.exists():
                    continue
                src = source_root / os.path.relpath(target, dst)
                if src.is_file():
                    link.unlink()
                    shutil.copy(src, link)
                else:
                    pending += 1
        if pending == 0:
            return


def seed_whisper() -> None:
    """models/whisper/: hub dir de faster-whisper + shards CAS que resuelvan.

    faster-whisper resuelve su modelo con cache_dir=download_root, así que
    necesita el layout hub-style bajo models/whisper/. El CAS de
    huggingface_hub 2.x guarda blobs grandes en shards compartidos fuera del
    dir por-modelo; copia el árbol y trae cada shard colgado.
    """
    repo = "Systran/faster-whisper-large-v2"
    hub_dir = HF_CACHE_DIR / "hub" / f"models--{repo.replace('/', '--')}"
    if not hub_dir.is_dir():
        sys.exit(f"Snapshot de {repo} ausente. Ejecuta make setup-models.")
    dst = STAGING_DIR / "whisper" / hub_dir.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_dir():
        shutil.rmtree(dst)
    shutil.copytree(hub_dir, dst, symlinks=True)
    _fix_dangling_symlinks(dst, hub_dir)
    print(f"✓ whisper/: seed listo ({dst.parent})")


def write_manifest() -> None:
    """models/MANIFEST.txt con ruta+sha256 relativas al staging."""
    import hashlib

    lines = [
        "textFlow staging manifest (make setup-models)",
    ]
    for f in sorted(STAGING_DIR.rglob("*")):
        if not f.is_file() or f.name == "MANIFEST.txt":
            continue
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        lines.append(f"  {f.relative_to(STAGING_DIR)}  sha256:{digest}")
    (STAGING_DIR / "MANIFEST.txt").write_text("\n".join(lines) + "\n")
    print(f"✓ MANIFEST ({len(lines) - 1} ficheros): {STAGING_DIR / 'MANIFEST.txt'}")


def run_assemblers(jobs: list[str]) -> None:
    """Ensambla los dirs planos exigidos por los workers (host, stdlib)."""
    if "gliner" in jobs:
        assemble_gliner()
        assemble_deberta()
    if "whisper" in jobs:
        seed_whisper()


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
        if not dispatch_hf_in_container(hf_selected):
            return 1
        ok &= True

    run_assemblers([j for j in jobs if j != "docling"])

    if "docling" in jobs:
        ok &= run_docling()

    if ok:
        write_manifest()

    print("\nResumen:", "✓ todo preparado" if ok else "✗ hubo fallos — revisa la salida")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
