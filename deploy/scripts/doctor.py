#!/usr/bin/env python3
"""Preflight doctor de textFlow: valida fails fast antes de `docker up`.

Checks: docker/compose, env canónico, vars de backends, sanity de URLs,
staging de modelos local, dangling symlinks del cache, volumen models-data
y reachability de backends DESDE la red del stack.

Uso: python3 deploy/scripts/doctor.py [--skip-network] [--skip-volume]
Exit 0 = todo PASS (con warnings posibles); 1 = algún FAIL.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PROJECT_ROOT / "deploy" / "docker" / "docker-compose.yml"
DEFAULT_ENV = PROJECT_ROOT / ".env"

REQUIRED_BACKEND_VARS = (
    "EMBEDDINGS_BASE_URL",
    "EMBEDDINGS_MODEL",
    "EMBEDDINGS_DIMENSION",
    "INFERENCE_BASE_URL",
    "INFERENCE_MODEL",
)

GLINER_FILES = (
    "config.json", "gliner_config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "spm.model", "added_tokens.json", "pytorch_model.bin",
)
DEBERTA_FILES = (
    "config.json", "spm.model", "tokenizer_config.json",
    "pytorch_model.bin", "special_tokens_map.json",
)

# id del check -> hint accionable
HINTS = {
    "docker": "instala docker + docker compose v2 (/docs/deployment-guide.md)",
    "env": "cp .env.example .env y rellénalo (bloque BACKENDS)",
    "backends": "rellena el bloque BACKENDS del .env — el worker correspondiente\n"
                "  no arranca sin ello (crash-loop verificado)",
    "urls": "quita el sufijo /v1 de las BASE_URL: los clientes lo añaden\n"
            "  ellos mismos (/v1/v1/embeddings = doble path = connection error)",
    "gliner": "make setup-models y elige 'gliner' (arma el dir plano con assets vendidos)",
    "deberta": "make setup-models 'gliner' también arma models/deberta-v3-small/",
    "whisper": "make setup-models y elige 'whisper' (solo lo necesitas para audio)",
    "docling": "make setup-models y elige 'docling' (extrae artefactos de la imagen)",
    "symlinks": "staging con symlinks colgados — re-ejecuta make setup-models",
    "volume": "make models-sync (puebla el volumen models-data)",
    "reach": "el backend no es alcanzable desde la red del stack.\n"
             "  ¿host? usa http://host.docker.internal:<puerto>; ¿revisa el .env?\n"
             "  re-corre tras arreglar EMBEDDINGS_*/INFERENCE_*",
    "host_gateway": "sin extra_hosts host-gateway los workers no alcanzan backends en el host",
}

# ------------------------- checks puros (unit-testeados) ---------------------


def parse_env(text: str) -> dict[str, str]:
    """dotenv mínimo: KEY=VALUE, sin export, con comentarios y comillas."""
    env: dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            env[k] = v
    return env


def check_required_vars(env: dict[str, str]) -> tuple[bool, list[str]]:
    missing = [k for k in REQUIRED_BACKEND_VARS if not (env.get(k) or "").strip()]
    return (not missing, missing)


def check_url_sanity(env: dict[str, str]) -> tuple[bool, str]:
    for key in ("EMBEDDINGS_BASE_URL", "INFERENCE_BASE_URL"):
        url = (env.get(key) or "").strip()
        if not url:
            continue
        if "/v1" in url.split("//", 1)[1]:
            return False, f"{key} lleva /v1 → los clientes lo añaden ellos: quita el sufijo"
    return True, "URLs sin sufijo /v1"


def check_staging(staging: Path) -> list[str]:
    """Dir plano gliner/deberta (script de fail) + whisper/docling (warn)."""
    problems: list[str] = []
    gl = staging / "gliner-small-v2.1"
    missing = [f for f in GLINER_FILES if not (gl / f).is_file()]
    if missing:
        problems.append(f"gliner-small-v2.1 incompleto (falta {', '.join(missing)})")
    db = staging / "deberta-v3-small"
    missing = [f for f in DEBERTA_FILES if not (db / f).is_file()]
    if missing:
        problems.append(f"deberta-v3-small incompleto (falta {', '.join(missing)})")
    wh_bin = (staging / "whisper" / "models--Systran--faster-whisper-large-v2"
              / "snapshots").glob("*/model.bin")
    if not next(wh_bin, None):
        problems.append("whisper: sin model.bin en models/whisper (warn: solo audio)")
    doc = staging / "docling"
    if not doc.is_dir() or sum(1 for _ in doc.glob("*")) == 0:
        problems.append("docling: sin artefactos en models/docling (warn: solo PDF/office)")
    return problems


# --------------------- checks de sistema (subprocess) -----------------------

RESULTS: list[tuple[str, bool, str]] = []


def record(cid: str, ok: bool, detail: str) -> None:
    RESULTS.append((cid, ok, detail))


def check_binary(argv: list[str], cid: str, label: str) -> bool:
    try:
        subprocess.run(argv, capture_output=True, timeout=20, check=True)
        record(cid, True, label)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        record(cid, False, f"{label}: {exc.__class__.__name__}")
        return False


def check_dangling_symlinks(staging: Path) -> None:
    out = subprocess.run(
        ["find", str(staging), "-type", "l", "-xtype", "l"],
        capture_output=True, text=True, timeout=30,
    )
    dangling = [ln for ln in out.stdout.splitlines() if ln.strip()]
    record("symlinks", not dangling,
           "sin symlinks colgados" if not dangling else f"{len(dangling)} symlinks colgados")


# id del check que puede fallar sin bloquear (warning, no error)
WARNABLE = {"whisper", "docling"}


def check_volume() -> None:
    # el volumen lo crea compose; si no existe aún, es info, no fallo duro
    out = subprocess.run(
        ["docker", "volume", "inspect", "docker_models-data"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        record("volume", False, "models-data no existe aún (make models-sync lo crea)")
        return
    probe = subprocess.run(
        ["docker", "run", "--rm", "-v", "docker_models-data:/m",
         "python:3.10-slim", "python", "-c",
         "import os,sys; sys.exit(0 if len(os.listdir('/m'))>=3 else 1)"],
        capture_output=True, timeout=60,
    )
    ok = probe.returncode == 0
    record("volume", ok, "volumen models-data poblado" if ok else "volumen vacío")


def _run_in_network(program: str, envs: dict[str, str], network: str) -> subprocess.CompletedProcess:
    argv = ["docker", "run", "--rm", "--network", network]
    # mismo mecanismo que los workers (extra_hosts host-gateway): sin esto el
    # one-shot no resuelve host.docker.internal y faltaría el fallo real
    argv += ["--add-host", "host.docker.internal:host-gateway"]
    for k, v in envs.items():
        argv += ["-e", f"{k}={v}"]
    argv += ["python:3.10-slim", "python", "-c", program]
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def probe_backend(cid: str, url: str, model: str, dimension: str | None,
                  network: str) -> None:
    """Un contenedor en la red del stack: /v1/models y, con dims, embeddings."""
    program = (
        "import json,os,sys,urllib.request\n"
        "base=os.environ['URL'].rstrip('/')\n"
        "def get(path):\n"
        "    return json.load(urllib.request.urlopen(base+path, timeout=20))\n"
        "names=[m.get('id') for m in get('/v1/models').get('data',[])]\n"
        "used=os.environ['MODEL']\n"
        "base_names={i.split(':')[0] for i in names}\n"
        "if used and used.split(':')[0] not in base_names:\n"
        "    print('modelo ausente en /v1/models:', used, names); sys.exit(1)\n"
        "if os.environ.get('DIMS'):\n"
        "    body=json.dumps({'model':used,'input':'doctor-probe'}).encode()\n"
        "    req=urllib.request.Request(base+'/v1/embeddings',data=body,\n"
        "        headers={'Content-Type':'application/json'})\n"
        "    vec=json.load(urllib.request.urlopen(req,timeout=30))['data'][0]['embedding']\n"
        "    if len(vec)!=int(os.environ['DIMS']):\n"
        "        print('dims',len(vec),'!= esperado',os.environ['DIMS']); sys.exit(2)\n"
        "print('ok')\n"
    )
    envs = {"URL": url, "MODEL": model}
    if dimension:
        envs["DIMS"] = dimension
    r = _run_in_network(program, envs, network)
    tail = (r.stdout or "").strip() or (r.stderr or "").strip()[-160:]
    record(cid, r.returncode == 0,
           f"backend OK ({url})" if r.returncode == 0 else f"{url}: {tail}")


def check_host_gateway_declared() -> None:
    """Los workers que hablan con host deben declarar extra_hosts host-gateway."""
    out = subprocess.run(
        ["docker", "compose", "--env-file", str(DEFAULT_ENV),
         "-f", str(COMPOSE_FILE), "config"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        record("host_gateway", False, "compose config no renderiza")
        return
    # el render normaliza ":" a "=" (host.docker.internal=host-gateway)
    n = out.stdout.count("host-gateway")
    record("host_gateway", n >= 3,
           f"host-gateway declarado en {n} workers" if n >= 3
           else f"solo {n}/3 workers con host-gateway (embeddings, completion, inference)")


def check_reachability(env: dict[str, str], network: str = "docker_default") -> bool:
    probe_backend("reach_embeddings", env["EMBEDDINGS_BASE_URL"].strip(),
                  env.get("EMBEDDINGS_MODEL", "").strip(),
                  env["EMBEDDINGS_DIMENSION"].strip(), network)
    probe_backend("reach_inference", env["INFERENCE_BASE_URL"].strip(),
                  env.get("INFERENCE_MODEL", "").strip(), None, network)
    return RESULTS[-2][1] and RESULTS[-1][1]


# --------------------------------- main -------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-network", action="store_true",
                    help="no sondear backends desde la red del stack")
    ap.add_argument("--skip-volume", action="store_true")
    ap.add_argument("--env", default=str(DEFAULT_ENV))
    args = ap.parse_args(argv)

    check_binary(["docker", "--version"], "docker", "docker instalado")
    check_binary(["docker", "compose", "version"], "compose", "docker compose v2")
    if not COMPOSE_FILE.is_file():
        record("compose", False, f"falta {COMPOSE_FILE}")

    env_path = Path(args.env)
    record("env", env_path.is_file(),
           ".env presente" if env_path.is_file() else ".env ausente en la raíz")
    env = parse_env(env_path.read_text()) if env_path.is_file() else {}

    backends_ok, missing = check_required_vars(env)
    record("backends", backends_ok,
           "vars backend completas" if backends_ok else f"faltan: {', '.join(missing)}")
    urls_ok, urls_msg = check_url_sanity(env)
    record("urls", urls_ok, urls_msg)

    staging = PROJECT_ROOT / "models"
    for cid, ok, detail in _staging_rows(check_staging(staging)):
        record(cid, ok, detail)

    if not args.skip_volume and RESULTS and RESULTS[1][1]:  # compose disponible
        check_volume()

    network_ok = backends_ok and urls_ok
    if not args.skip_network and network_ok:
        check_host_gateway_declared()
        check_reachability(env)
    elif not args.skip_network:
        record("reach", False, "saltado: completa backends/urls y re-corre")

    print("\n═══ textFlow doctor ═══")
    fails = 0
    for cid, ok, detail in RESULTS:
        warn = not ok and cid in WARNABLE
        icon = "✅" if ok else ("⚠️ " if warn else "❌")
        fails += 0 if ok or warn else 1
        print(f"{icon} {cid:<16} {detail}")
        if not ok and cid in HINTS:
            print(f"   └→ {HINTS[cid]}")
    print(f"\nfails={fails}")
    return 1 if fails else 0


def _staging_rows(problems: list[str]):
    """Convierte los problem-string de check_staging en filas (cid, ok, detail)."""
    row = []
    for cid, prefix in (("gliner", "gliner-small-v2.1"), ("deberta", "deberta-v3-small"),
                        ("whisper", "whisper:"), ("docling", "docling:")):
        hit = [p for p in problems if p.startswith(prefix)]
        if not hit:
            row.append((cid, True, f"{prefix.rstrip(':')}: staging íntegro"))
        else:
            warn = " (warn" in hit[0]
            row.append((cid, warn, hit[0]))
    return row


if __name__ == "__main__":
    sys.exit(main())
