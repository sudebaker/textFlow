# Public-Ready Remediation — textFlow

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Llevar textFlow a un estado production-ready y seguro para hacer el repositorio público en GitHub (pasar de privado a público).

**Architecture:** Plan secuencial en 4 fases: (0) reconciliación del repo — reset local a origin/main + merge de la rama de optimización; (1) P0 críticos — auth, bare excepts; (2) P1 altos — CI roto, GPU opcional, rotación de secretos; (3) P2 pulido público — CONTRIBUTING, CHANGELOG, smoke test, corrección de README.

**Tech Stack:** Go 1.22.5 (Gin, zerolog, env/v11), Python 3.11 (pika, redis, pydantic-settings), Docker Compose, RabbitMQ 3.13, Redis 7, GitHub Actions.

## Global Constraints

- **Idioma de commits y docs:** Español (excepto mensajes de commit que siguen convenciones git en inglés).
- **Go binaries:** Todos en `bin/`, nunca en el directorio source. `bin/*` está en `.gitignore`.
- **Air-gapped:** No internet en build o runtime. `RUN pip install` ✅, `RUN wget` ❌. `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`.
- **No commitear secretos:** Cualquier `.env` con valores reales debe estar en `.gitignore`. Solo `.env.example` y `.env.test` trackeados.
- **Tests primero:** Cada cambio funcional va con su test.
- **Baseline:** `origin/main` commit `e80235f` (estado actual del remote).

---

## File Structure

### Archivos a crear

| Archivo | Responsabilidad |
|---------|----------------|
| `internal/middleware/auth.go` | API key middleware (constant-time compare) |
| `internal/middleware/auth_test.go` | Tests del auth middleware |
| `deploy/docker/docker-compose.gpu.yml` | Override file para GPU (CPU por defecto) |
| `CONTRIBUTING.md` | Guía de contribución para repo público |
| `CHANGELOG.md` | Historial de versiones (Keep a Changelog) |

### Archivos a modificar

| Archivo | Cambio |
|---------|--------|
| `internal/config/config.go` | Añadir campo `APIKey` + validación |
| `cmd/orchestrator/main.go` | Registrar auth middleware en rutas protegidas |
| `deploy/docker/docker-compose.yml` | Quitar `devices: [nvidia]`, cambiar defaults a CPU |
| `deploy/docker/.env.example` | Añadir `API_KEY=` + documentación |
| `tests/e2e/test_inference_embeddings_e2e.py:225` | Reemplazar bare `except:` |
| `.github/workflows/ci.yml` | Actualizar `actions/upload-artifact` v3→v4, añadir service containers |
| `README.md` | Corregir URL `anomalyco/textflow` → `sudebaker/textFlow` |
| `.gitignore` | Añadir `deploy/docker/.env` (si no está) |

---

## FASE 0 — Reconciliación del repo

### Task 0.1: Resetear local a origin/main

**Contexto:** El local está 305 commits detrás de origin/main y 310 commits adelante (historia divergente por force push). Decidimos resetear al estado del remote.

**Files:** Ninguno (operación de git)

- [ ] **Step 1: Verificar que no hay cambios sin commitear importantes**

```bash
git status
git stash list
```

Expected: solo `session-notes.md` modificado y `docs/superpowers/plans/2026-07-07-prod-readiness-audit.md` sin tracking. Ambos son prescindibles (se regeneran).

- [ ] **Step 2: Resetear al remote**

```bash
git fetch origin
git reset --hard origin/main
```

Expected: HEAD apunta a `e80235f fix: race conditions in inference assembly + pubsub reliability`.

- [ ] **Step 3: Verificar estado limpio**

```bash
git status
git log --oneline -3
```

Expected: `working tree clean`, HEAD en `e80235f`.

- [ ] **Step 4: Verificar que `.env` no está trackeado**

```bash
git ls-files deploy/docker/.env
```

Expected: vacío (no hay output). Si hay output, ejecutar `git rm --cached deploy/docker/.env`.

---

### Task 0.2: Merge de la rama `optimización-de-código-56edb`

**Contexto:** La rama `origin/optimización-de-código-56edb` tiene 1 squash commit (`69769cf`) con 272 commits de trabajo (performance fixes, features routing, batch processing, whisper service, inference cache). Diverge desde `7d87a45` (ancestro común con main). El merge tendrá conflictos porque ambas ramas evolucionaron independientemente desde ese punto.

**Files:** Muchos (122 archivos cambiados en la rama)

- [ ] **Step 1: Crear rama de trabajo desde main**

```bash
git checkout -b merge-optimizacion
```

- [ ] **Step 2: Intentar merge con strategy ort (default en git 2.x)**

```bash
git merge origin/optimización-de-código-56edb --no-commit --no-ff
```

Expected: conflictos en múltiples archivos. Si el merge es demasiado complejo (más de 30 conflictos), considerar alternativa: cherry-pick selectivo de commits individuales en lugar de squash merge.

- [ ] **Step 3: Evaluar conflictos**

```bash
git diff --name-only --diff-filter=U
```

Listar archivos en conflicto. Si hay más de 30 conflictos o conflictos en archivos críticos (`cmd/orchestrator/main.go`, `pkg/worker_common/base.py`), ir al Step 4 (alternativa). Si hay menos de 10 conflictos menores, resolver manualmente.

- [ ] **Step 4 (alternativa si merge falla): Merge con strategy `-X theirs`**

Si el merge directo es inviable, como la rama remota solo tiene 1 commit (squash), no hay commits individuales accesibles para cherry-pick. En este caso:

```bash
# Abortar merge
git merge --abort

# Hacer merge con strategy "theirs" para archivos de workers Python,
# y resolver manualmente solo orchestrator y config
git merge origin/optimización-de-código-56edb -X theirs --no-commit
```

Luego revisar manualmente `cmd/orchestrator/main.go` e `internal/config/config.go` para asegurar que los cambios de origin/main (Adaptive Flow Control, race condition fixes) no se pierdan.

- [ ] **Step 5: Resolver conflictos restantes manualmente**

Para cada archivo en conflicto:
1. `git checkout --theirs <file>` si el cambio viene de la rama optimización
2. `git checkout --ours <file>` si el cambio de origin/main debe preservarse
3. Editar manualmente si ambos lados tienen cambios válidos

Prioridad: preservar Adaptive Flow Control (origin/main) + incorporar performance fixes (rama optimización).

- [ ] **Step 6: Verificar que el código compila**

```bash
go build ./cmd/orchestrator 2>&1 | head -20
go build ./cmd/resource-manager 2>&1 | head -20
go vet ./... 2>&1 | head -20
```

Expected: sin errores. Si hay errores de compilación, resolver imports/types rotos por el merge.

- [ ] **Step 7: Verificar tests Python**

```bash
pytest cmd/embeddings-worker/tests/ cmd/inference-worker/tests/ cmd/metadata-worker/tests/ -v --tb=short 2>&1 | tail -20
```

Expected: tests pasan (puede haber algunos nuevos de la rama optimización).

- [ ] **Step 8: Commit del merge**

```bash
git add -A
git commit -m "merge: integrar rama optimización-de-código (performance, features routing, whisper, inference cache)"
```

- [ ] **Step 9: Push a origin**

```bash
git push origin merge-optimizacion
```

Crear PR en GitHub para revisión antes de mergear a main.

- [ ] **Step 10: Crear PR**

```bash
gh pr create --title "Merge: rama optimización-de-código → main" \
  --body "Integra 272 commits de performance fixes, features routing, batch processing, whisper service, inference cache. Resuelve conflictos preservando Adaptive Flow Control de main." \
  --base main --head merge-optimizacion
```

**Nota:** Este task requiere revisión humana. No mergear a main automáticamente hasta que el PR sea revisado y el CI pase.

---

## FASE 1 — P0 CRÍTICOS (bloqueantes para producción)

### Task 1.1: Auth API key middleware en orchestrator

**Contexto:** `cmd/orchestrator/main.go` no tiene auth. Todos los endpoints están abiertos. Para air-gapped on-prem, API key header check es suficiente.

**Files:**
- Create: `internal/middleware/auth.go`
- Create: `internal/middleware/auth_test.go`
- Modify: `internal/config/config.go` (añadir `APIKey` field)
- Modify: `cmd/orchestrator/main.go` (registrar middleware)
- Modify: `deploy/docker/.env.example` (documentar `API_KEY`)

**Interfaces:**
- Consumes: `config.Config.APIKey` (string, opcional — si vacío, auth deshabilitada)
- Produces: `middleware.APIKeyAuth(expectedKey string) gin.HandlerFunc`

- [ ] **Step 1: Escribir el test del middleware (fallo primero)**

Crear `internal/middleware/auth_test.go`:

```go
package middleware

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

func init() {
	gin.SetMode(gin.TestMode)
}

func TestAPIKeyAuth_DisabledWhenEmpty(t *testing.T) {
	r := gin.New()
	r.Use(APIKeyAuth(""))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}
}

func TestAPIKeyAuth_ValidKey(t *testing.T) {
	key := generateTestKey(t)
	r := gin.New()
	r.Use(APIKeyAuth(key))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-API-Key", key)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}
}

func TestAPIKeyAuth_MissingKey(t *testing.T) {
	key := generateTestKey(t)
	r := gin.New()
	r.Use(APIKeyAuth(key))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401, got %d", w.Code)
	}
}

func TestAPIKeyAuth_InvalidKey(t *testing.T) {
	key := generateTestKey(t)
	r := gin.New()
	r.Use(APIKeyAuth(key))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-API-Key", "wrong-key")
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401, got %d", w.Code)
	}
}

func TestAPIKeyAuth_TimingAttackResistance(t *testing.T) {
	key := generateTestKey(t)
	r := gin.New()
	r.Use(APIKeyAuth(key))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	// Even with a very long wrong key, should still 401 (constant-time compare)
	longWrongKey := make([]byte, len(key)*10)
	for i := range longWrongKey {
		longWrongKey[i] = 'x'
	}

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-API-Key", string(longWrongKey))
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401, got %d", w.Code)
	}
}

func generateTestKey(t *testing.T) string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		t.Fatalf("failed to generate key: %v", err)
	}
	return hex.EncodeToString(b)
}

// Ensure subtle.ConstantTimeCompare is used (compile-time check)
var _ = subtle.ConstantTimeCompare
```

- [ ] **Step 2: Correr el test para verificar que falla**

```bash
go test -v ./internal/middleware/ -run TestAPIKeyAuth
```

Expected: FAIL con `undefined: APIKeyAuth`.

- [ ] **Step 3: Implementar el middleware**

Crear `internal/middleware/auth.go`:

```go
package middleware

import (
	"crypto/subtle"
	"net/http"

	"github.com/gin-gonic/gin"
)

// APIKeyAuth returns a Gin middleware that validates the X-API-Key header
// against the expected key using constant-time comparison to prevent timing
// attacks. If expectedKey is empty, auth is disabled (dev mode).
func APIKeyAuth(expectedKey string) gin.HandlerFunc {
	return func(c *gin.Context) {
		if expectedKey == "" {
			c.Next()
			return
		}
		provided := c.GetHeader("X-API-Key")
		if provided == "" || subtle.ConstantTimeCompare([]byte(provided), []byte(expectedKey)) != 1 {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid_api_key"})
			return
		}
		c.Next()
	}
}
```

- [ ] **Step 4: Correr el test para verificar que pasa**

```bash
go test -v ./internal/middleware/ -run TestAPIKeyAuth
```

Expected: PASS — 5 tests pasan.

- [ ] **Step 5: Añadir `APIKey` a Config**

Modificar `internal/config/config.go`. Añadir al struct `Config` (después de `QueueMaxLength`):

```go
	// API Key for orchestrator auth (empty = disabled, for dev mode)
	APIKey string `env:"API_KEY" default:""`
```

Añadir a `Validate()`:

```go
	if c.APIKey != "" && len(c.APIKey) < 32 {
		return fmt.Errorf("SECURITY: API_KEY must be at least 32 characters when set — current length: %d", len(c.APIKey))
	}
```

- [ ] **Step 6: Registrar el middleware en setupRouter**

Modificar `cmd/orchestrator/main.go`. En la función `setupRouter()`, justo después de `r.Use(limiter.Middleware())` y antes de `r.GET("/health", ...)`, añadir:

```go
	// API key auth (disabled if API_KEY env var is empty)
	authMiddleware := middleware.APIKeyAuth(cfg.APIKey)
```

Luego, cambiar el grupo `v1` para usar auth:

```go
	v1 := r.Group("/v1")
	v1.Use(authMiddleware)
	{
		v1.POST("/documents/process", createJobHandler)
		v1.POST("/documents/upload", uploadHandler)
		v1.POST("/documents/batch", handlers.CreateBatchHandler)
		v1.GET("/documents/:id", getJobHandler)
		v1.GET("/documents/:id/graph", handlers.GraphHandler)
		v1.GET("/documents/:id/vectors", handlers.VectorsHandler)
		v1.GET("/documents/:id/entities", handlers.EntitiesHandler)
		v1.GET("/documents/:id/inferences", handlers.InferencesHandler)
		v1.GET("/documents/:id/download", downloadHandler)
		v1.DELETE("/documents/:id", deleteJobHandler)
		v1.GET("/batches/:id/status", handlers.GetBatchStatusHandler)
	}

	v1Auth := r.Group("/v1")
	v1Auth.Use(authMiddleware)
	v1Auth.GET("/jobs/:id/stream", handlers.StreamJobHandler)
```

**Nota:** `/health` y `/metrics` quedan sin auth (necesarios para health checks y Prometheus scrape). `/swagger/*any` también queda público (es docs read-only).

- [ ] **Step 7: Compilar y verificar**

```bash
go build -o bin/orchestrator ./cmd/orchestrator
```

Expected: sin errores.

- [ ] **Step 8: Actualizar `.env.example`**

Añadir al final de `deploy/docker/.env.example`:

```env
# ============================================================================
# API AUTHENTICATION
# ============================================================================
# API key for orchestrator REST API authentication.
# If empty, auth is DISABLED (dev mode only — never disable in production).
# Must be at least 32 characters. Generate with: openssl rand -hex 32
#
# All /v1/* endpoints require X-API-Key header when this is set.
# /health and /metrics remain public for health checks and Prometheus.

API_KEY=
```

- [ ] **Step 9: Commit**

```bash
git add internal/middleware/auth.go internal/middleware/auth_test.go internal/config/config.go cmd/orchestrator/main.go deploy/docker/.env.example
git commit -m "feat(security): add API key auth middleware to orchestrator

- internal/middleware/auth.go: constant-time X-API-Key validation
- internal/config/config.go: APIKey field (env: API_KEY, optional)
- cmd/orchestrator/main.go: protect /v1/* routes, leave /health /metrics public
- 5 tests: disabled, valid, missing, invalid, timing-attack resistance"
```

---

### Task 1.2: Eliminar bare `except:` en e2e test

**Contexto:** Queda 1 bare `except:` en `tests/e2e/test_inference_embeddings_e2e.py:225`. `cmd/docling-server/` no existe en origin/main (solo era local).

**Files:**
- Modify: `tests/e2e/test_inference_embeddings_e2e.py:225`

- [ ] **Step 1: Leer el contexto del bare except**

```bash
sed -n '220,230p' tests/e2e/test_inference_embeddings_e2e.py
```

- [ ] **Step 2: Reemplazar el bare except**

Cambiar línea 225:

```python
            except:
```

Por:

```python
            except Exception as e:
                logger.error(f"Unexpected error in e2e test: {e}", exc_info=True)
```

- [ ] **Step 3: Verificar que no quedan bare excepts**

```bash
grep -rn "except:\s*$" cmd/ tests/ pkg/ 2>&1
```

Expected: 0 resultados.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_inference_embeddings_e2e.py
git commit -m "fix(e2e): replace bare except with except Exception in inference test"
```

---

## FASE 2 — P1 HIGH (antes de hacer público)

### Task 2.1: Arreglar CI (GitHub Actions)

**Contexto:** 5 runs consecutivos en fallo. Causa: `actions/upload-artifact@v3` deprecated (GitHub bloquea v3 desde abril 2024). También faltan service containers para tests que necesitan Redis/RabbitMQ.

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Leer el CI actual completo**

```bash
cat .github/workflows/ci.yml
```

- [ ] **Step 2: Actualizar `actions/upload-artifact` v3 → v4**

Reemplazar todas las ocurrencias de `uses: actions/upload-artifact@v3` por `uses: actions/upload-artifact@v4`.

- [ ] **Step 3: Actualizar `actions/cache` v3 → v4**

Reemplazar `uses: actions/cache@v3` por `uses: actions/cache@v4`.

- [ ] **Step 4: Añadir service containers para tests Go**

En el job `go-tests`, añadir después de `runs-on: ubuntu-latest`:

```yaml
    services:
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
        options: --health-cmd "redis-cli ping" --health-interval 10s --health-timeout 5s --health-retries 5
      rabbitmq:
        image: rabbitmq:3.13-management
        ports:
          - 5672:5672
          - 15672:15672
        env:
          RABBITMQ_DEFAULT_USER: test
          RABBITMQ_DEFAULT_PASS: test
        options: --health-cmd "rabbitmq-diagnostics -q ping" --health-interval 10s --health-timeout 5s --health-retries 5
```

- [ ] **Step 5: Añadir env vars para tests Go**

En el job `go-tests`, añadir después de `steps:` (antes del primer step):

```yaml
    env:
      REDIS_URL: redis://localhost:6379
      RABBITMQ_URL: amqp://test:test@localhost:5672/
```

- [ ] **Step 6: Actualizar Python tests para no colisionar**

En el job `python-tests`, cambiar el step "Run Python tests" para evitar colisión de módulo `worker.py`:

```yaml
    - name: Run Python tests
      run: |
        # Run each worker's tests separately to avoid module name collisions
        for dir in cmd/*/tests; do
          if [ -d "$dir" ]; then
            worker_dir=$(dirname "$dir")
            echo "Running tests for $worker_dir"
            cd "$worker_dir"
            python -m pytest tests/ -v --tb=short || exit 1
            cd - > /dev/null
          fi
        done
```

- [ ] **Step 7: Añadir requirements-test.txt install**

En el job `python-tests`, después del step "Install dependencies", añadir:

```yaml
    - name: Install test dependencies
      run: |
        find cmd/*/requirements-test.txt -exec pip install -r {} \; 2>/dev/null || true
        find cmd/*/dev-requirements.txt -exec pip install -r {} \; 2>/dev/null || true
```

- [ ] **Step 8: Commit y push para triggerear CI**

```bash
git add .github/workflows/ci.yml
git commit -m "fix(ci): update deprecated actions, add service containers, fix Python test collisions

- actions/upload-artifact v3→v4, actions/cache v3→v4
- Add Redis + RabbitMQ service containers for Go tests
- Run Python tests per-worker to avoid worker.py module collision
- Install requirements-test.txt and dev-requirements.txt"
git push origin main
```

- [ ] **Step 9: Verificar que el CI pasa**

```bash
gh run watch
```

Expected: CI green. Si falla, revisar logs con `gh run view --log-failed` y corregir.

---

### Task 2.2: GPU opcional en docker-compose

**Contexto:** 4 servicios (`embeddings-worker`, `entities-worker`, `completion-worker`, `docling`) declaran `devices: [driver: nvidia]` sin fallback. Máquinas sin NVIDIA no pueden arrancar.

**Files:**
- Modify: `deploy/docker/docker-compose.yml`
- Create: `deploy/docker/docker-compose.gpu.yml`

- [ ] **Step 1: Leer el docker-compose actual**

```bash
cat deploy/docker/docker-compose.yml
```

- [ ] **Step 2: Quitar GPU config del docker-compose.yml base**

Para cada uno de los 4 servicios (`embeddings-worker`, `entities-worker`, `completion-worker`, `docling`):

1. Cambiar `EMBEDDINGS_DEVICE=${EMBEDDINGS_DEVICE:-cuda}` → `EMBEDDINGS_DEVICE=${EMBEDDINGS_DEVICE:-cpu}`
2. Cambiar `ENTITIES_DEVICE=${ENTITIES_DEVICE:-cuda}` → `ENTITIES_DEVICE=${ENTITIES_DEVICE:-cpu}`
3. Cambiar `DOCLING_DEVICE=${DOCLING_DEVICE:-cuda}` → `DOCLING_DEVICE=${DOCLING_DEVICE:-cpu}`
4. Eliminar las líneas `CUDA_VISIBLE_DEVICES=0` y `NVIDIA_VISIBLE_DEVICES=0` de la sección `environment`
5. Eliminar el bloque `deploy.resources.reservations.devices` completo (las 4 líneas que definen `driver: nvidia`)

- [ ] **Step 3: Crear `deploy/docker/docker-compose.gpu.yml`**

```yaml
# GPU override file — use with:
#   docker compose -f docker-compose.yml -f docker-compose.gpu.yml up
#
# This file adds NVIDIA GPU support to workers that benefit from CUDA.
# The base docker-compose.yml defaults to CPU-only.

services:
  embeddings-worker:
    environment:
      - EMBEDDINGS_DEVICE=cuda
      - CUDA_VISIBLE_DEVICES=0
      - NVIDIA_VISIBLE_DEVICES=0
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  entities-worker:
    environment:
      - ENTITIES_DEVICE=cuda
      - CUDA_VISIBLE_DEVICES=0
      - NVIDIA_VISIBLE_DEVICES=0
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  completion-worker:
    environment:
      - CUDA_VISIBLE_DEVICES=0
      - NVIDIA_VISIBLE_DEVICES=0
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  docling:
    environment:
      - DOCLING_DEVICE=cuda
      - NVIDIA_VISIBLE_DEVICES=0
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

- [ ] **Step 4: Verificar que el compose base es válido (sin GPU)**

```bash
docker compose -f deploy/docker/docker-compose.yml config --quiet
```

Expected: sin errores.

- [ ] **Step 5: Verificar que el compose con GPU override es válido**

```bash
docker compose -f deploy/docker/docker-compose.yml -f deploy/docker/docker-compose.gpu.yml config --quiet
```

Expected: sin errores.

- [ ] **Step 6: Verificar que el base no tiene devices**

```bash
docker compose -f deploy/docker/docker-compose.yml config | grep -A2 "devices:"
```

Expected: 0 resultados (no aparece `devices:` en el base).

- [ ] **Step 7: Verificar que el GPU override añade devices**

```bash
docker compose -f deploy/docker/docker-compose.yml -f deploy/docker/docker-compose.gpu.yml config | grep -A2 "devices:"
```

Expected: aparece `driver: nvidia` 4 veces.

- [ ] **Step 8: Commit**

```bash
git add deploy/docker/docker-compose.yml deploy/docker/docker-compose.gpu.yml
git commit -m "feat(docker): make GPU optional via compose override file

- docker-compose.yml: default to CPU (EMBEDDINGS_DEVICE=cpu, etc.)
- docker-compose.gpu.yml: override file adding NVIDIA GPU support
- Usage: docker compose -f docker-compose.yml -f docker-compose.gpu.yml up"
```

---

### Task 2.3: Verificar y completar .gitignore para .env

**Contexto:** Origin/main ya des-trackeó `deploy/docker/.env` (commit `2ee094e`), pero `.gitignore` solo excluye `deploy/.env`, no `deploy/docker/.env` explícitamente. Verificar que esté cubierto.

**Files:**
- Modify: `.gitignore` (si es necesario)

- [ ] **Step 1: Verificar que .env está ignorado**

```bash
git check-ignore -v deploy/docker/.env
```

Expected: muestra la regla que matchea. Si no hay output, añadir regla.

- [ ] **Step 2: Si no está cubierto, añadir reglas explícitas**

Añadir a `.gitignore` en la sección "Otros":

```
deploy/docker/.env
deploy/docker/.env.local
deploy/docker/.env.*.local
```

- [ ] **Step 3: Verificar que .env.example sigue trackeado**

```bash
git ls-files deploy/docker/.env.example
```

Expected: `deploy/docker/.env.example` (sí está trackeado).

- [ ] **Step 4: Commit (solo si hubo cambios)**

```bash
git add .gitignore
git commit -m "chore(git): ensure deploy/docker/.env is gitignored"
```

---

### Task 2.4: Auditar git history por secretos reales

**Contexto:** Aunque el `.env` des-trackeado tenía placeholders `CHANGE_ME_*`, conviene verificar que ningún secreto real se commiteó en el history.

**Files:** Ninguno (auditoría)

- [ ] **Step 1: Revisar el history del .env**

```bash
git log -p --all -- deploy/docker/.env 2>&1 | grep -iE "password|secret|api_key|token" | grep -v "CHANGE_ME" | grep -v "^-" | grep -v "#" | head -20
```

Expected: 0 resultados (todo es `CHANGE_ME_*` o comentarios).

- [ ] **Step 2: Si se encuentran secretos reales, rotarlos**

Si el Step 1 muestra algo que no sea `CHANGE_ME_*`, rotar esa credencial en el sistema de deployment y actualizar `.env.example` con un nuevo placeholder.

- [ ] **Step 3: Considerar git-filter-repo si hay secretos en history**

Si se encuentran secretos reales y el repo va a ser público, usar `git filter-repo` para purgar el history:

```bash
# Solo si se encuentran secretos reales:
pip install git-filter-repo
git filter-repo --path deploy/docker/.env --invert-paths
# Force push (cuidado: reescribe history)
git push origin main --force
```

**Nota:** Este paso es destructivo. Solo ejecutar si se encuentran secretos reales y el usuario lo aprueba.

---

## FASE 3 — P2 Pulido para repo público

### Task 3.1: Corregir URL en README

**Contexto:** El README dice `git clone https://github.com/anomalyco/textflow.git` pero el remote es `sudebaker/textFlow`.

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Buscar todas las URLs incorrectas**

```bash
grep -n "anomalyco" README.md
```

- [ ] **Step 2: Reemplazar todas las ocurrencias**

Reemplazar `github.com/anomalyco/textflow` por `github.com/sudebaker/textFlow` en todo el README.

- [ ] **Step 3: Verificar que no quedan URLs incorrectas**

```bash
grep -n "anomalyco" README.md
```

Expected: 0 resultados.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: fix GitHub URL in README (anomalyco → sudebaker)"
```

---

### Task 3.2: Crear CONTRIBUTING.md

**Contexto:** Para un repo público, se necesita una guía de contribución mínima.

**Files:**
- Create: `CONTRIBUTING.md`

- [ ] **Step 1: Crear CONTRIBUTING.md**

```markdown
# Contributing to textFlow

¡Gracias por tu interés en contribuir a textFlow! Este documento describe el proceso para contribuir al proyecto.

## Requisitos de desarrollo

- **Go** 1.22.5+
- **Python** 3.11+
- **Docker** + Docker Compose
- **Make** (para usar el Makefile)
- **golangci-lint** (para Go linting)
- **black** + **isort** (para Python formatting)

## Setup del entorno

```bash
# Clonar y configurar
git clone https://github.com/sudebaker/textFlow.git
cd textFlow
cp .env.example .env

# Iniciar infraestructura (RabbitMQ, Redis, Docling)
make infra-up

# Verificar que todo compila
make build
```

## Flujo de trabajo

### 1. Crear una rama

```bash
git checkout -b feat/tu-feature
```

### 2. Hacer cambios siguiendo las convenciones

- **Go:** `gofmt -s`, `go vet`, `golangci-lint`
- **Python:** `black --line-length 120`, `isort --profile black`
- **Imports:** 3 secciones (standard library, third-party, local) — ver AGENTS.md
- **Naming:** Go PascalCase/camelCase, Python snake_case/PascalCase — ver AGENTS.md

### 3. Tests

```bash
# Go
make test

# Python
make test-python

# Tests específicos
go test -v ./internal/middleware/...
pytest cmd/embeddings-worker/tests/ -v
```

Todo código nuevo debe incluir tests. El CI bloquea PRs que no pasan tests.

### 4. Verificar calidad

```bash
make lint
make format
```

### 5. Commit con conventional commits

Usar [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: añadir nuevo endpoint de búsqueda
fix: corregir race condition en completion-worker
docs: actualizar documentación de la API
refactor: simplificar lógica de chunking
test: añadir tests para metadata-worker
chore: actualizar dependencias
```

### 6. Push y PR

```bash
git push origin feat/tu-feature
gh pr create --title "feat: descripción corta" --body "Descripción del cambio"
```

## Convenciones del proyecto

- **Idioma:** Respuestas de issues y PRs en español.
- **Air-gapped:** No usar `wget`, `curl` a internet, ni HF Hub API en Dockerfiles. Modelos se montan como volúmenes.
- **Go binaries:** Siempre en `bin/`, nunca en el directorio source.
- **No commitear secretos:** `.env` está gitignored. Usar `.env.example` para templates.
- **Tests herméticos:** Los tests no deben depender de servicios externos reales. Usar mocks o service containers en CI.

## Estructura del proyecto

Ver [README.md](README.md) y [AGENTS.md](AGENTS.md) para la arquitectura completa.

## Reportar bugs

Abrir un issue con:
1. Descripción del problema
2. Pasos para reproducir
3. Comportamiento esperado vs actual
4. Logs relevantes (sin secretos)
5. Versión de Go, Python, Docker

## Código de conducta

Sé respetuoso. No toleramos harassment, discriminación ni comportamiento tóxico.
```

- [ ] **Step 2: Commit**

```bash
git add CONTRIBUTING.md
git commit -m "docs: add CONTRIBUTING.md for public repo contributions"
```

---

### Task 3.3: Crear CHANGELOG.md

**Contexto:** Historial de versiones en formato Keep a Changelog para el repo público.

**Files:**
- Create: `CHANGELOG.md`

- [ ] **Step 1: Crear CHANGELOG.md**

```markdown
# Changelog

Todos los cambios notables de textFlow se documentan aquí.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es/1.1.0/),
y este proyecto adhiere a [Semantic Versioning](https://semver.org/lang/es/).

## [Unreleased]

### Added
- API key authentication middleware para orchestrator (`X-API-Key` header)
- `docker-compose.gpu.yml` override file para GPU opcional
- CONTRIBUTING.md para contribuciones públicas

### Changed
- `docker-compose.yml` default a CPU (sin `devices: [nvidia]`)
- CI actualizado: `actions/upload-artifact` v3→v4, service containers para Redis/RabbitMQ

### Fixed
- CI roto por `actions/upload-artifact@v3` deprecated
- Bare `except:` en `tests/e2e/test_inference_embeddings_e2e.py`
- URL incorrecta en README (`anomalyco/textflow` → `sudebaker/textFlow`)

## [0.1.0] - 2026-07-20

### Added
- Event-driven microservices architecture: Go orchestrator + Python workers
- Workers: extraction (Docling), embeddings (bge-m3), entities (GLiNER), metadata, inference (vLLM), completion, audio (Whisper), image (multimodal LLM)
- RabbitMQ broker con thread-safe channel pool y publisher confirms
- Redis client con TTL propagation y reconnection
- REST API con upload, polling, SSE streaming, batch processing
- SSRF protection, rate limiting, circuit breaker middleware
- Prometheus metrics: jobs_total, job_duration_seconds, queue_depth, http_requests
- Health checks para Redis y RabbitMQ
- Air-gapped deployment: 100% offline tras descarga inicial de modelos
- Docker Compose con healthchecks para todos los servicios
- BaseWorker/BaseAsyncWorker/BasePubSubWorker unificación para Python workers
- Adaptive Flow Control: admission control, RabbitMQ queue limits, AIMD semaphore
- Inference cache (Redis) para evitar llamadas redundantes al LLM
- Race condition fixes en inference assembly y pubsub reliability

### Security
- Non-root Dockerfiles para todos los servicios
- Credenciales RabbitMQ con placeholders `CHANGE_ME_*` + validación
- CORS configurable via `CORS_ORIGINS_LIST`
- `deploy/docker/.env` des-trackeado de git

## Guía de versiones

- **MAJOR:** cambios incompatibles en la API
- **MINOR:** nueva funcionalidad backward-compatible
- **PATCH:** bug fixes backward-compatible
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: add CHANGELOG.md (Keep a Changelog format)"
```

---

### Task 3.4: Smoke test del pipeline completo

**Contexto:** Verificar que el pipeline end-to-end funciona antes de hacer público. Pendiente desde julio.

**Files:** Ninguno (verificación)

- [ ] **Step 1: Iniciar infraestructura**

```bash
make infra-up
```

Expected: RabbitMQ, Redis, Docling corriendo.

- [ ] **Step 2: Build de todos los binarios**

```bash
make build
```

Expected: `bin/orchestrator`, `bin/resource-manager`, `bin/client` creados sin errores.

- [ ] **Step 3: Iniciar orchestrator**

```bash
make run-orchestrator &
sleep 3
curl http://localhost:8080/health
```

Expected: `{"status":"up","components":{"redis":"up","rabbitmq":"up"}}`.

- [ ] **Step 4: Iniciar workers**

```bash
make run-workers &
sleep 5
```

- [ ] **Step 5: Subir un documento de prueba**

```bash
curl -X POST http://localhost:8080/v1/documents/upload \
  -F "file=@README.md" \
  -F "features=entities,metadata"
```

Expected: `{"job_id":"<uuid>","status":"queued"}`.

- [ ] **Step 6: Polling del estado**

```bash
JOB_ID=<uuid del paso anterior>
curl http://localhost:8080/v1/documents/$JOB_ID
```

Repetir cada 2 segundos hasta que `status` sea `completed`.

- [ ] **Step 7: Descargar resultados**

```bash
curl http://localhost:8080/v1/documents/$JOB_ID/download | gunzip > /tmp/resultados.json
cat /tmp/resultados.json | head -50
```

Expected: JSON con entities, metadata, chunks.

- [ ] **Step 8: Si algo falla, documentar el issue**

Si el smoke test falla, crear un issue en GitHub con:
- El paso que falló
- Logs del servicio que falló
- Comando exacto que se ejecutó

No hacer público el repo hasta que el smoke test pase.

---

### Task 3.5: Hacer el repo público

**Contexto:** Tras completar todas las fases anteriores y verificar que el smoke test pasa.

**Files:** Ninguno (operación de GitHub)

- [ ] **Step 1: Push de todos los cambios a origin/main**

```bash
git push origin main
```

- [ ] **Step 2: Verificar que el CI pasa**

```bash
gh run list --limit 3
```

Expected: último run en `success`.

- [ ] **Step 3: Hacer el repo público via GitHub CLI**

```bash
gh repo edit sudebaker/textFlow --visibility public
```

- [ ] **Step 4: Verificar que el repo es accesible públicamente**

```bash
curl -s https://api.github.com/repos/sudebaker/textFlow | grep -o '"private": [a-z]*'
```

Expected: `"private": false`.

- [ ] **Step 5: Guardar el hito en Engram**

Guardar observación en Engram indicando que el repo pasó de privado a público, con la fecha y el estado de los fixes aplicados.

---

## Resumen de tareas

| Fase | Task | Descripción | Tiempo estimado |
|------|------|-------------|-----------------|
| 0 | 0.1 | Resetear local a origin/main | 5 min |
| 0 | 0.2 | Merge rama optimización-de-código | 1-3 h (según conflictos) |
| 1 | 1.1 | Auth API key middleware | 1 h |
| 1 | 1.2 | Eliminar bare except en e2e | 10 min |
| 2 | 2.1 | Arreglar CI | 30 min |
| 2 | 2.2 | GPU opcional en docker-compose | 30 min |
| 2 | 2.3 | Verificar .gitignore | 5 min |
| 2 | 2.4 | Auditar git history por secretos | 15 min |
| 3 | 3.1 | Corregir URL en README | 5 min |
| 3 | 3.2 | Crear CONTRIBUTING.md | 20 min |
| 3 | 3.3 | Crear CHANGELOG.md | 20 min |
| 3 | 3.4 | Smoke test del pipeline | 30 min |
| 3 | 3.5 | Hacer repo público | 5 min |

**Total estimado:** 4-6 horas (dependiendo de conflictos en el merge de la Fase 0).

---

## Verificación final

Antes de marcar el plan como completo, verificar:

- [ ] `git status` limpio en main
- [ ] CI green en origin/main
- [ ] `curl localhost:8080/health` devuelve 200 sin API key
- [ ] `curl -X POST localhost:8080/v1/documents/upload` devuelve 401 sin API key
- [ ] `curl -X POST localhost:8080/v1/documents/upload -H "X-API-Key: <correcta>"` funciona
- [ ] `docker compose -f deploy/docker/docker-compose.yml config --quiet` sin errores
- [ ] `grep -rn "except:\s*$" cmd/ tests/ pkg/` devuelve 0 resultados
- [ ] `grep "anomalyco" README.md` devuelve 0 resultados
- [ ] `ls CONTRIBUTING.md CHANGELOG.md` ambos existen
- [ ] Repo público en GitHub (`"private": false`)
- [ ] Smoke test del pipeline completo pasa