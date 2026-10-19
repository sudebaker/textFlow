# textFlow — Especificación revisada: extracción visual para documentos manuscritos

**Estado:** Plan de implementación revisado tras inspección del codebase  
**Objetivo:** añadir recuperación visual de páginas difíciles, especialmente manuscritas, manteniendo intacto el contrato externo del pipeline.

---

## 1. Veredicto arquitectónico

La arquitectura propuesta originalmente es válida en sus principios:

- salida canónica única;
- reemplazo de extracción, no fusión;
- fallback visual;
- benchmark antes de fijar umbrales;
- quality gate inicialmente heurístico;
- desacoplamiento del resto del pipeline.

Pero la implementación real del repositorio obliga a corregir el planteamiento.

### Hecho fundamental

Actualmente `extraction-worker` **no dispone de una representación textual por página**.

`_docling_convert_async` produce un blob documental (`md_content`/`text_content`) y la información `document.pages` se utiliza únicamente como conteo.

Por tanto, la arquitectura real debe ser:

```text
                 FAST PATH
PDF
 │
 ▼
Docling
 │
 ▼
document-level pre-gate
 │
 ├── PASS ───────────────► devolver salida actual intacta
 │
 └── SUSPECT
          │
          ▼
      materializar páginas
          │
          ▼
      page-level gate
          │
          ├── PASS ─────► Docling page text
          │
          └── FAIL/SUSPECT
                    │
                    ▼
              Vision OCR
                    │
                    ▼
              MiniCPM-V 4.5
                    │
                    ▼
             document assembly
                    │
                    ▼
             salida canónica
```

Esto convierte la modificación en **cirugía mayor interna del `extraction-worker` con contrato externo intacto**.

---

# 2. Restricciones descubiertas en el codebase

## 2.1. No existe extracción por página

Actualmente:

- Docling devuelve un único blob;
- `document.pages` se cuenta y descarta;
- downstream trabaja sobre el blob.

### Consecuencia

No se debe intentar introducir un page pipeline para todos los documentos desde el principio.

Debe existir una **puerta de dos niveles**.

### Nivel 1 — documento

Utilizar señales baratas sobre el blob ya generado:

- caracteres por página;
- densidad de texto;
- placeholders de imagen;
- ratio de basura;
- otras señales derivadas del resultado actual.

Si el documento pasa:

> devolver `md_content`/`text_content` exactamente como hoy.

### Nivel 2 — página

Solo si el documento resulta `SUSPECT`:

- utilizar los bytes originales ya disponibles;
- renderizar páginas con `pypdfium2`;
- evaluar cada página;
- enviar a Vision OCR únicamente las páginas necesarias.

No debe existir una segunda descarga del documento.

---

# 3. Contrato externo

El contrato del extraction-worker hacia downstream debe permanecer intacto.

No modificar:

- `analyze_text`;
- `chunk_text`;
- `SourceClassifier`;
- extractor de entidades;
- embeddings;
- workers downstream.

El cambio debe estar completamente encapsulado dentro de extraction.

---

# 4. Golden test obligatorio

Antes de activar cualquier nueva funcionalidad:

```text
VISION_OCR_ENABLED=false
```

debe producir una salida **byte-idéntica** a la implementación actual.

Esto debe convertirse en un test automático.

### Criterio

```text
flag OFF
   ↓
output nuevo == output baseline
```

No basta con igualdad semántica.

La regresión debe verificarse a nivel de bytes donde el contrato actual lo permita.

Este test es la barrera principal para garantizar que el nuevo sistema no afecta al 90% de documentos que no necesitan visión.

---

# 5. Arquitectura interna

## 5.1. Backend de extracción

Introducir una abstracción conceptual:

```text
ExtractionBackend
 ├── DoclingBackend
 └── VisionOCRBackend
       └── MiniCPMBackend
```

LightOnOCR se deja preparado pero no debe formar parte del primer incremento.

## 5.2. Page extraction

La representación por página se introduce únicamente dentro de la ruta lenta.

Conceptualmente:

```text
Raw PDF bytes
     │
     ▼
pypdfium2
     │
     ├── page 1
     ├── page 2
     ├── page 3
     └── ...
```

Cada página debe poder asociarse a:

```text
page_number
image
docling_result
vision_result
final_text
provenance
```

---

# 6. Fase A — Verificaciones previas

La mayor parte del análisis arquitectónico ya está realizada.

Antes de tocar implementación deben verificarse únicamente:

1. campos disponibles por página en Docling-serve 0.12;
2. inventario real de GPUs y VRAM;
3. soporte de la versión pinneada de vLLM para MiniCPM-V 4.5;
4. disponibilidad offline de los pesos;
5. mecanismo exacto de montaje de modelos en el entorno air-gap.

Estas verificaciones no deben bloquear el diseño de las fases A-C salvo que aparezca una incompatibilidad real.

---

# 7. Fase B — Materialización por página

Implementar la capacidad de convertir los bytes originales del PDF en imágenes de página mediante `pypdfium2`.

## Requisitos

- utilizar los bytes ya presentes en el worker;
- no descargar nuevamente el documento;
- conservar orientación;
- permitir seleccionar resolución/DPI;
- evitar materializar páginas si el documento pasa el pre-gate.

## Fast path

```text
Docling
 ↓
pre-gate PASS
 ↓
return original output
```

No debe ejecutarse:

- render;
- pypdfium2;
- Vision OCR;
- assembly adicional.

---

# 8. Fase C — Quality Gate en modo log-only

Esta fase debe desplegarse **antes de construir e integrar Vision OCR**.

Inicialmente el gate no modifica comportamiento.

Solo observa:

```text
document
   ↓
quality gate
   ↓
logs/metrics
```

## Objetivo

Medir con tráfico real:

- porcentaje PASS;
- porcentaje SUSPECT;
- distribución por tamaño;
- relación con tipo de documento;
- falsos positivos observables;
- falsos negativos observables;
- páginas potencialmente problemáticas.

Esto permite calibrar los umbrales con datos reales antes de activar fallback.

## Métricas

Como mínimo:

```text
pages_total
documents_total

quality_gate_pass
quality_gate_suspect

documents_suspect
estimated_pages_suspect

text_density
image_placeholder_ratio
garbage_ratio
```

Los nombres definitivos deben seguir la convención existente.

---

# 9. Fase D — Servicio `vision-ocr`

El repositorio ya dispone de un patrón de servicio OCR-vision en:

```text
deploy/docker/image-analyzer/app/main.py
```

Debe reutilizarse conceptualmente.

## Estrategia

Crear:

```text
deploy/docker/vision-ocr
```

como servicio específico para MiniCPM-V 4.5.

### Comunicación

```text
extraction-worker
      │
      │ HTTP
      ▼
vision-ocr
      │
      ▼
vLLM
      │
      ▼
MiniCPM-V 4.5
```

No introducir una cola RabbitMQ nueva.

Esto evita añadir complejidad de mensajería y mantiene el patrón de comunicación HTTP ya existente en otros servicios.

## API

Debe ser curl-testable desde el primer día.

Preferiblemente mantener interfaz OpenAI-compatible si el servicio existente y el runtime lo permiten.

---

# 10. Prompt de MiniCPM

El objetivo es transcripción, no interpretación.

Prompt conceptual:

```text
Extract ALL text visible in the image.
Transcribe it exactly.
Preserve the original language.
Do not translate.
Do not summarize.
Do not infer missing text.
If text is illegible, leave it empty rather than inventing content.
```

Configuración inicial:

```text
temperature = 0
```

El prompt definitivo debe congelarse después del benchmark.

---

# 11. Resolución de imagen

No heredar automáticamente:

```text
MAX_IMAGE_DIM=1024
```

del `image-analyzer`.

Para manuscritos puede ser demasiado agresivo.

El servicio `vision-ocr` debe comenzar con:

```text
sin resize por defecto
```

y dejar que el benchmark determine la configuración adecuada.

Primer benchmark:

```text
120 DPI
200 DPI
300 DPI
```

La configuración final se decidirá mediante:

- calidad;
- latencia;
- VRAM;
- throughput.

---

# 12. Cache

El servicio debe reutilizar el patrón existente de cache por SHA256 de imagen/página.

Conceptualmente:

```text
page image
    ↓
SHA256
    ↓
Redis
    ├── hit  → resultado
    └── miss → MiniCPM → cache → resultado
```

Esto permite:

- evitar inferencias repetidas;
- acelerar reintentos;
- facilitar benchmark;
- abaratar reprocesamientos.

---

# 13. Air-gap

MiniCPM-V 4.5 debe funcionar sin acceso externo.

Los pesos:

- deben descargarse previamente;
- quedar disponibles localmente;
- montarse read-only;
- estar incluidos en el procedimiento de despliegue air-gap.

Debe verificarse explícitamente que la versión pinneada de vLLM soporta la versión concreta de MiniCPM-V 4.5.

No asumir compatibilidad solo por pertenecer a la misma familia.

---

# 14. Fase E — Integración worker + Vision OCR

Una vez disponible el servicio:

```text
document
   ↓
Docling
   ↓
document pre-gate
   │
   ├── PASS → salida actual
   │
   └── SUSPECT
          ↓
       render pages
          ↓
       page gate
          │
          ├── PASS → Docling
          │
          └── SUSPECT/FAIL
                    ↓
                 MiniCPM
```

El fallback debe estar protegido por:

```text
VISION_OCR_ENABLED=false
```

hasta completar el benchmark.

---

# 15. Fase F — Semántica de reemplazo

No fusionar:

```text
Docling text + MiniCPM text
```

porque produce duplicación y ambigüedad.

La regla es:

```text
Docling OK
    → Docling

Docling insuficiente
    → MiniCPM reemplaza resultado de página
```

El ensamblado final conserva el orden original.

---

# 16. Semántica de errores

El comportamiento actual del worker es estricto: una excepción no controlada puede marcar el job como failed y hacer `nack` sin requeue.

Por tanto, la ruta Vision OCR necesita un `try/except` estrecho y específico.

## Error de Vision OCR

Debe:

1. registrar el error;
2. escribir provenance `vision_error`;
3. conservar el texto Docling disponible para esa página;
4. continuar el documento.

Ejemplo:

```text
Docling
  ↓
quality fail
  ↓
MiniCPM error
  ↓
fallback → Docling original
  ↓
provenance = vision_error
```

## Si ambas rutas fallan

El guard de texto vacío existente continúa siendo la última defensa.

En ese caso el job puede fallar según la política actual.

---

# 17. Provenance

La procedencia es información de control pequeña y job-scoped.

Debe almacenarse en Redis, no en FSStore.

Conceptualmente:

```text
orchestrator:job:{id}:extraction_provenance
```

Por página:

```json
{
  "page": 17,
  "backend": "minicpm-v4.5",
  "status": "fallback",
  "reason": "quality_gate"
}
```

Otros estados posibles:

```text
docling
vision
vision_error
degraded
```

La estructura exacta debe adaptarse a los modelos existentes.

FSStore continúa reservado para blobs grandes.

---

# 18. Presupuesto temporal

El pipeline actual tiene:

```text
JobTimeout = 60 min
DOCLING_CONVERSION_TIMEOUT = 1800 s
```

Por tanto no se puede asumir que todas las páginas problemáticas podrán pasar por Vision OCR.

Ejemplo:

```text
Docling = 25 min
100 páginas × 5-15 s
       = 8-25 min
--------------------
total = 33-50 min
```

Sin presupuesto explícito existe riesgo de superar el timeout global.

## Solución

Introducir:

- timeout por página;
- máximo de páginas Vision por documento;
- máximo de tiempo Vision por documento;
- cancelación cooperativa entre páginas;
- estado `degraded` cuando se agota el presupuesto.

Debe reutilizarse `_raise_if_cancelled` donde corresponda.

---

# 19. Degraded

Cuando el documento excede el presupuesto:

```text
páginas restantes
        ↓
no ejecutar Vision
        ↓
conservar resultado disponible
        ↓
provenance = degraded
```

No debe bloquearse indefinidamente un documento por intentar recuperar cada página.

---

# 20. Backpressure y concurrencia

El repositorio ya dispone de `adaptive_semaphore.py` y un patrón AIMD en `inference-worker`.

Debe reutilizarse el conocimiento existente en lugar de crear otro mecanismo.

La comunicación esperada:

```text
worker
   │
   ▼
vision-ocr
   │
   ├── capacity
   └── 503/retry
```

La concurrencia debe estar limitada en **dos niveles**:

### Cliente

El extraction-worker no debe lanzar trabajo ilimitado.

### Servidor

`vision-ocr` debe tener un semáforo server-side.

Esto es obligatorio porque la GPU debe estar protegida incluso si algún cliente se comporta mal.

---

# 21. Contención GPU

Antes de fijar la configuración de MiniCPM hay que inventariar:

- número de GPUs;
- VRAM;
- modelos residentes;
- carga habitual;
- GPU asignada a cada servicio;
- política del resource-manager.

Hay que considerar explícitamente la coexistencia de:

```text
bge-m3
GLiNER
LLM inferencias
Whisper
image-analyzer
MiniCPM-V
```

## Decisión

Con una única GPU limitada:

- evaluar cuantización AWQ;
- o concurrencia estrictamente limitada;
- o aislamiento de GPU si existe más de una.

La selección no debe hacerse por memoria teórica del modelo, sino mediante prueba real de serving.

---

# 22. Configuración

Seguir el patrón actual del worker:

```python
os.getenv(...)
```

No introducir Pydantic solo para esta funcionalidad.

Configuración conceptual:

```text
VISION_OCR_ENABLED=false
VISION_OCR_URL=...
VISION_OCR_MODEL=...
VISION_OCR_TIMEOUT=...
VISION_OCR_MAX_PAGES_PER_DOCUMENT=...
VISION_OCR_MAX_SECONDS_PER_DOCUMENT=...
VISION_OCR_MAX_CONCURRENCY=...
VISION_OCR_IMAGE_DPI=...
VISION_OCR_QUALITY_THRESHOLD=...
```

Los nombres finales deben respetar las convenciones existentes.

---

# 23. Fase F — Benchmark inicial

Empezar con un corpus pequeño:

```text
10-30 páginas por categoría
```

Categorías:

- manuscrito limpio;
- manuscrito difícil;
- escaneado;
- PDF imagen;
- mixto;
- texto digital;
- layout complejo.

No intentar transcribir manualmente cientos de páginas.

## Métricas

Prioridad:

1. calidad downstream de entidades;
2. calidad de texto;
3. latencia;
4. VRAM;
5. throughput.

CER/WER:

- utilizar únicamente en un subconjunto con ground truth;
- no convertir la generación de ground truth completa en cuello de botella.

---

# 24. Resoluciones del benchmark

Comparar inicialmente:

```text
120 DPI
200 DPI
300 DPI
```

y, si los resultados lo justifican, ampliar.

Para cada resolución registrar:

```text
OCR quality
entity quality
latency
VRAM
tokens
throughput
```

---

# 25. LightOnOCR

LightOnOCR se mantiene como backend secundario.

No debe bloquear MiniCPM.

Su inclusión en producción debe depender del benchmark.

Debe evaluarse especialmente en:

- texto impreso;
- coordenadas;
- velocidad;
- consumo;
- documentos donde la información espacial sea relevante.

No asumir que será mejor para manuscritos.

Si MiniCPM domina claramente el caso central, LightOnOCR no necesita convertirse en otra pieza de infraestructura simplemente porque existe.

---

# 26. Fotografías sueltas

Las fotografías `.jpg/.png` no forman parte inicialmente de este cambio.

El orchestrator ya las enruta a `image-worker` mediante:

```text
image_replaces_extraction
```

y `image-analyzer` ya cubre esa ruta.

## Futuro

Puede unificarse el backend apuntando `image-worker` al mismo `vision-ocr`, pero eso queda fuera del alcance de esta entrega.

El alcance actual es:

> PDFs/documentos cuyo contenido problemático aparece en páginas.

---

# 27. Observabilidad

Añadir métricas para:

```text
documents_total
documents_suspect

pages_total
pages_docling
pages_vision

quality_gate_pass
quality_gate_suspect
quality_gate_fail

vision_success
vision_error
vision_degraded

vision_processing_seconds
vision_queue_wait_seconds
```

También debe poder medirse el porcentaje de documentos que abandonan el fast path.

---

# 28. Criterios de aceptación

## Regresión

- [ ] `VISION_OCR_ENABLED=false` produce salida byte-idéntica al baseline.
- [ ] Existe golden test automático.
- [ ] El fast path no renderiza páginas.
- [ ] No se modifican workers downstream.

## Arquitectura

- [ ] La materialización por página ocurre solo tras document-level SUSPECT.
- [ ] Se reutilizan los bytes originales.
- [ ] No se descarga nuevamente el documento.
- [ ] No se crean colas RabbitMQ nuevas.
- [ ] Vision OCR se comunica mediante HTTP.

## Serving

- [ ] `vision-ocr` funciona standalone mediante curl.
- [ ] Existe semáforo server-side.
- [ ] El servicio funciona offline.
- [ ] Imagen Docker construible con `--network=none`.
- [ ] Pesos montados read-only.
- [ ] Versión de vLLM validada para MiniCPM-V 4.5.

## Calidad

- [ ] Quality gate desplegado inicialmente en log-only.
- [ ] Existen métricas reales antes de activar fallback.
- [ ] El benchmark incluye manuscritos.
- [ ] Se mide calidad downstream de entidades.

## Errores

- [ ] Error de Vision no mata automáticamente el job.
- [ ] Existe provenance `vision_error`.
- [ ] Se conserva el resultado Docling cuando sea posible.
- [ ] El job falla únicamente cuando no existe resultado utilizable y la política actual así lo exige.

## Presupuesto

- [ ] Existe timeout por página.
- [ ] Existe presupuesto máximo de páginas Vision.
- [ ] Existe presupuesto máximo de tiempo Vision.
- [ ] Existe cancelación cooperativa.
- [ ] Se puede marcar `degraded`.

## GPU

- [ ] Se ha inventariado VRAM disponible.
- [ ] Se ha medido memoria real del serving.
- [ ] La concurrencia está limitada.
- [ ] Se ha comprobado interacción con los servicios GPU existentes.

---

# 29. Orden de ejecución definitivo

El orden recomendado es:

```text
A. Verificaciones finales
       │
       ▼
B. Materialización por página
   + provenance
   + golden test
       │
       ▼
C. Quality Gate LOG-ONLY
       │
       ├──────────────┐
       │              │
       ▼              ▼
   datos reales    D. vision-ocr
                    MiniCPM
                       │
                       ▼
                  E. integración
                       │
                       ▼
                  F. benchmark
                       │
                       ▼
                  G. LightOnOCR
                    (si procede)
                       │
                       ▼
                  H. hardening
```

### Reordenamiento crítico

**C debe desplegarse antes de activar D en producción.**

Esto reduce el riesgo porque permite calibrar el problema real sin introducir todavía una nueva ruta de inferencia.

D puede desarrollarse en paralelo con C, pero no debe activarse como fallback hasta disponer de datos del gate y de un serving validado.

---

# 30. Decisiones abiertas

No bloquean el arranque de A-C:

1. número y VRAM de GPUs;
2. disponibilidad offline de MiniCPM-V 4.5;
3. versión de vLLM compatible;
4. latencia aceptable para un manuscrito de 20 páginas;
5. resolución final;
6. límites de páginas/segundos Vision;
7. necesidad real de LightOnOCR.

---

# 31. Resultado esperado

La arquitectura final debe preservar el fast path:

```text
PDF
 ↓
Docling
 ↓
PASS
 ↓
salida actual
```

y añadir una ruta lenta únicamente cuando sea necesaria:

```text
PDF
 ↓
Docling
 ↓
SUSPECT
 ↓
render páginas
 ↓
page quality gate
 ↓
MiniCPM solo donde sea necesario
 ↓
replacement
 ↓
assembly
 ↓
salida canónica
```

El cambio es internamente importante, pero externamente invisible.

El resto de textFlow continúa recibiendo exactamente el mismo contrato.

---

# 32. Resumen ejecutivo

La propuesta revisada convierte la idea inicial de "añadir OCR visual" en una evolución controlada del extraction-worker.

Las cuatro decisiones estructurales son:

1. **No asumir representación por página donde actualmente no existe.**
2. **Mantener un fast path documental que preserve exactamente la salida actual.**
3. **Desplegar el quality gate en log-only antes de activar Vision OCR.**
4. **Reutilizar el patrón existente de image-analyzer para crear `vision-ocr`, sin introducir nuevas colas.**

MiniCPM-V 4.5 es el primer candidato para manuscritos.

LightOnOCR queda condicionado al benchmark.

El sistema debe ser tolerante a fallos, consciente del presupuesto temporal y protegido frente a contención de GPU.

La arquitectura downstream permanece intacta.
