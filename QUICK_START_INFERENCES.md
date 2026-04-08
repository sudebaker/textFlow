# Quick Start: Inferencias en Imágenes y Audio

## TL;DR

El cliente Go ahora soporta inferencias en imágenes y audio usando el flag `-f`.

### Uso Básico

```bash
# Imagen con inferencias
./bin/client -i photo.jpg -o results.json -f

# Audio con inferencias
./bin/client -i audio.mp3 -o results.json -f

# Audio + Diarización + Inferencias
./bin/client -i recording.wav -o results.json -f --diarize

# Sin inferencias (comportamiento anterior)
./bin/client -i photo.jpg -o results.json
```

---

## Qué Cambió

| Antes | Ahora |
|-------|-------|
| `-f` solo funcionaba para documentos (JSON/base64) | `-f` ahora funciona para imágenes Y audio |
| Imágenes/audio se procesaban SIN inferencias | Imágenes/audio pueden incluir inferencias |
| Resultados NO contenían campo `inferences` | Resultados CONTIENEN campo `inferences` en chunks |

---

## Implementación

### Cliente (tools/client/main.go)
- ✅ Envía campo `features=inferences` en formulario multipart cuando se usa `-f`
- ✅ Funciona para imágenes (.jpg, .jpeg, .png)
- ✅ Funciona para audio (.mp3, .wav, .m4a, .ogg)

### Orchestrator (cmd/orchestrator/main.go)
- ✅ Lee campo `features` del formulario multipart
- ✅ Almacena en Redis: `orchestrator:job:{id}:features`
- ✅ El resto del pipeline lo utiliza automáticamente

---

## Verificación Rápida

```bash
# 1. Compilar
make build

# 2. Iniciar servicios
make infra-up
make run-orchestrator

# 3. En otra terminal, procesar imagen con inferencias
./bin/client -i test_image.png -o results.json -f

# 4. Verificar features en Redis (reemplaza <job_id> con el de la salida)
redis-cli GET "orchestrator:job:<job_id>:features"
# Esperado: ["inferences"]

# 5. Ver resultado
cat results.json | jq '.chunks[0].inferences'
```

---

## Estructura del Resultado

```json
{
  "job_id": "abc123...",
  "status": "completed",
  "chunks": [
    {
      "chunk_id": "chunk_0",
      "text": "...",
      "inferences": [
        {
          "text": "fact extracted from chunk",
          "confidence": 0.95,
          "entity_refs": ["entity_id_1"]
        }
      ]
    }
  ]
}
```

---

## Tests

Ejecutar tests automatizados:

```bash
# Todos los tests
bash test_client_inferences.sh

# Test específico
bash test_client_inferences.sh test_image_inferences
bash test_client_inferences.sh test_audio_inferences
```

---

## Troubleshooting

**Q: ¿Por qué mis resultados no tienen `inferences`?**

A: Verificar:
1. ¿Usaste flag `-f`?
   ```bash
   ./bin/client -i photo.jpg -o results.json -f
   ```
2. ¿Compilaste los cambios?
   ```bash
   make build
   ```
3. ¿Está corriendo inference-worker?
   ```bash
   make run-inference-worker
   ```

**Q: ¿Redis no contiene features?**

A: Verificar:
1. Orchestrator compilado correctamente: `make build-orchestrator`
2. Logs del orchestrator: `docker logs orchestrator | grep -i features`
3. Job realmente se creó con el ID correcto

**Q: ¿Necesito cambiar los workers?**

A: No, los workers existentes ya soportan features:
- `entities-worker` lee features y publica en cola inferences
- `inference-worker` procesa y genera micro-inferences
- `completion-worker` espera paso inferences si está en features

---

## Documentación Completa

Para documentación detallada, ver: `VERIFICATION_INFERENCES_CLIENT.md`

Incluye:
- Diagramas de flujo completos
- Claves Redis detalladas
- Todos los test cases
- Guía de troubleshooting exhaustiva
- Información de commits

---

## Backward Compatibility

✅ Código 100% backward compatible:
- Clientes viejos sin `-f` siguen funcionando igual
- Resultados sin inferencias siguen siendo válidos
- No hay cambios en API REST

