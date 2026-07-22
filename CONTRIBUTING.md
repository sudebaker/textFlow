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
git clone https://github.com/sudebaker/textFlow.git
cd textFlow
cp .env.example .env

make infra-up
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
- **Imports:** 3 secciones (standard library, third-party, local)
- **Naming:** Go PascalCase/camelCase, Python snake_case/PascalCase

### 3. Tests

```bash
make test
make test-python
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

- **Idioma:** Issues y PRs en español.
- **Air-gapped:** No usar `wget`, `curl` a internet, ni HF Hub API en Dockerfiles. Modelos se montan como volúmenes.
- **Go binaries:** Siempre en `bin/`, nunca en el directorio source.
- **No commitear secretos:** `.env` está gitignored. Usar `.env.example` para templates.
- **Tests herméticos:** No depender de servicios externos reales. Usar mocks o service containers en CI.

## Reportar bugs

Abrir un issue con:
1. Descripción del problema
2. Pasos para reproducir
3. Comportamiento esperado vs actual
4. Logs relevantes (sin secretos)
5. Versión de Go, Python, Docker

## Código de conducta

Sé respetuoso. No toleramos harassment, discriminación ni comportamiento tóxico.
