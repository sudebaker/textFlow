# Whisper Service Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adaptar el cliente HTTP del audio-worker para que sea compatible con la API del nuevo servicio Whisper (`faster-whisper` vía FastAPI) y limpiar código muerto no usado. Plan completado y ejecutado correctamente.

**Architecture:** El audio-worker ya existe y funciona. El problema son dos incompatibilidades de campos entre el cliente y el nuevo servicio. Son cambios quirúrgicos en `pkg/audio_client/client.py` más actualización de tests. Un cambio adicional: eliminación del fallback `_transcribe_onerahmet` que estaba interfiriendo con el test failover y que no se usa con el nuevo servicio.

**Tech Stack:** Python 3.11, `requests`, `pytest`, `pytest-mock`

**Execution:**
- Task 1: Tests updated (commit 0369e41) — 11 tests, all passing
- Task 2: Client fixed (commit c96a9f7) — form field, duration key, port 8080
- Task 3: Legacy code removed (commit c96a9f7) — 61 líneas eliminadas (`_transcribe_onerahmet`, `_transcribe_single`)

**Summary:**
| Fichero | Líneas | Cambio |
|---|---|---|
| `pkg/audio_client/client.py` | -61 | Eliminación de fallback onerahmet + simplificación |
| `pkg/audio_client/tests/test_client.py` | +2 | 2 nuevos tests, 3 actualizaciones, 2 correcciones de puerto |

---

## Incompatibilidades detectadas

| Elemento | Cliente actual | Nuevo whisper-service |
|---|---|---|
| Form field del audio | `"file"` | `"audio"` |
| Campo duración en response | `"duration_seconds"` | `"duration"` |
| Puerto default | `9000` | `8080` |

**Nota:** El fallback `_transcribe_onerahmet` interceptaba la secuencia de reintentos y causaba tests que parecían fallar por "texto vacío". Solución: eliminarlo, ya que el nuevo servicio solo usa `/transcribe`.
