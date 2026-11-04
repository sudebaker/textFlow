# textFlow — Especificación: extracción visual para documentos manuscritos

**Estado:** Propuesta de implementación  
**Objetivo:** incorporar recuperación de texto mediante modelos de visión para documentos manuscritos y otros documentos visualmente difíciles, sin alterar el contrato del pipeline posterior de textFlow.

---

## 1. Objetivo

Actualmente textFlow funciona correctamente cuando el documento contiene texto digital o cuando los extractores convencionales consiguen recuperar el contenido.

El problema aparece con documentos manuscritos, escaneos difíciles, fotografías y páginas en las que el texto visual no puede ser recuperado de forma fiable por Docling/OCR convencional.

La solución propuesta es ampliar `extraction-worker` con una ruta de extracción visual capaz de recuperar texto de estas páginas y entregar el resultado al resto del pipeline exactamente en el formato canónico existente.

### Objetivo principal

```text
Documento
    ↓
extracción convencional
    ↓
evaluación de calidad por página
    ├── suficiente → aceptar
    └── insuficiente/sospechosa → extracción visual
    ↓
ensamblado del documento
    ↓
texto canónico existente
    ↓
pipeline actual de textFlow
```

El extractor de entidades, embeddings y demás fases posteriores **no deben necesitar conocer qué backend produjo el texto**.

---

## 2. Alcance

### Incluido

- Detección de páginas que requieren recuperación visual.
- Abstracción `VisionOCRBackend`.
- Integración inicial de **MiniCPM-V 4.5**.
- Preparación de **LightOnOCR** como backend alternativo.
- Procesamiento a nivel de página.
- Renderizado de páginas PDF a imagen cuando sea necesario.
- Evaluación de calidad de la extracción convencional.
- Fusión de resultados en un documento canónico.
- Metadata de procedencia y motivo del fallback.
- Benchmark específico de documentos manuscritos.
- Métricas de calidad y rendimiento.
- Trazabilidad suficiente para depuración.

### Fuera de alcance inicial

- Cambios en el extractor de entidades.
- Cambios en embeddings.
- Rediseño del pipeline posterior.
- Uso de un agente LLM para decidir el extractor.
- Clasificación sofisticada mediante modelos adicionales.
- Procesamiento visual obligatorio de todos los documentos.
- Sustitución global de Docling.
- Optimización prematura de throughput antes del benchmark.

---

# 3. Principios arquitectónicos

## 3.1. El `extraction-worker` sigue siendo el único responsable

La complejidad debe permanecer encapsulada en el extractor.

El resto de textFlow debe seguir viendo:

```text
document → canonical extraction result
```

y no:

```text
document → Docling/MiniCPM/OCR/... → ...
```

## 3.2. El fallback se decide por página

No debe asumirse que un documento entero es bueno o malo.

Ejemplo:

```text
Página 1 → Docling → OK
Página 2 → Docling → OK
Página 3 → Docling → SUSPECT → MiniCPM
Página 4 → Docling → OK
Página 5 → imagen → MiniCPM
```

Esto evita ejecutar inferencia visual innecesariamente sobre documentos largos.

## 3.3. El backend visual no modifica el contrato downstream

MiniCPM debe sustituir el mecanismo de recuperación de texto, no crear una segunda arquitectura de procesamiento documental.

## 3.4. Conservar procedencia

Debe ser posible saber, al menos:

```text
page
backend
quality_status
fallback_reason
```

Esto permitirá analizar posteriormente cuánto se utiliza el fallback y dónde falla.

---

# 4. Arquitectura propuesta

```text
                         textFlow
                            │
                            ▼
                    ┌───────────────┐
                    │ extraction    │
                    │    worker     │
                    └───────┬───────┘
                            │
                       PDF / imagen
                            │
                            ▼
                    ┌───────────────┐
                    │ Page Analyzer │
                    └───────┬───────┘
                            │
                     ┌──────┴──────┐
                     │             │
                  normal        difficult
                     │             │
                     ▼             ▼
                 Docling       Vision OCR
                                   │
                         ┌─────────┴─────────┐
                         │                   │
                    MiniCPM-V 4.5       LightOnOCR
                         │                   │
                         └─────────┬─────────┘
                                   │
                                   ▼
                           Page Text Result
                                   │
                                   ▼
                           Document Assembly
                                   │
                                   ▼
                         Canonical extraction
                                   │
                    ┌──────────────┴─────────────┐
                    ▼                            ▼
             Entity extractor              resto pipeline
```

---

# 5. Fase 0 — Inspección del estado actual

Antes de implementar, revisar el estado real del código de `extraction-worker`.

### Objetivos

Identificar:

- punto de entrada actual;
- contrato de entrada;
- contrato de salida;
- cómo se representan las páginas;
- dónde se ejecuta Docling;
- dónde se genera el texto canónico;
- cómo se propagan errores;
- dónde sería más limpio introducir el backend abstraction;
- mecanismo actual de persistencia de artefactos;
- configuración y gestión de modelos;
- límites actuales de concurrencia.

### Criterio de aceptación

La fase termina con una identificación concreta de los puntos de integración.

No modificar código estructuralmente hasta haber confirmado estos puntos contra la implementación actual.

---

# 6. Fase 1 — Definir el contrato `ExtractionBackend`

Introducir una abstracción interna para los distintos métodos de extracción.

Conceptualmente:

```text
ExtractionBackend
 ├── DoclingBackend
 ├── OCRBackend
 └── VisionOCRBackend
       ├── MiniCPMBackend
       └── LightOnOCRBackend
```

El nombre exacto y la firma deben adaptarse al estilo existente del proyecto.

## Resultado conceptual

Cada backend debe producir un resultado homogéneo:

```text
PageExtractionResult
```

con, como mínimo:

```text
page
text
backend
status
quality
fallback_reason
```

Los nombres exactos de los campos deben ajustarse al modelo de datos existente.

### Criterio de aceptación

Es posible sustituir el backend sin que el consumidor downstream tenga que conocer su implementación.

---

# 7. Fase 2 — Page Analyzer / Quality Gate

Crear una capa responsable de determinar si el resultado convencional de una página es suficientemente fiable.

No debe limitarse a:

```text
¿hay texto?
```

Debe aproximarse a:

```text
¿el texto recuperado parece suficientemente útil para el pipeline posterior?
```

## Señales iniciales

Usar inicialmente heurísticas simples y observables:

- existencia de texto nativo;
- longitud del texto;
- densidad de caracteres;
- cantidad de palabras;
- porcentaje de caracteres inválidos;
- fragmentación;
- páginas sin contenido;
- texto claramente corrupto;
- duplicaciones anómalas;
- presencia de imágenes;
- proporción de página ocupada por imágenes;
- resultado del OCR convencional cuando esté disponible.

## Estados

Se recomienda inicialmente:

```text
PASS
SUSPECT
FAIL
```

### PASS

La extracción convencional se acepta.

### SUSPECT

Hay texto, pero existen señales de posible pérdida o corrupción.

### FAIL

No se ha recuperado contenido útil.

## Importante

No introducir inicialmente un clasificador LLM/ML.

Primero recoger datos reales y comprobar qué señales son predictivas.

---

# 8. Fase 3 — Clasificación básica del tipo de página

No es necesario construir inicialmente un clasificador sofisticado.

El sistema debe poder inferir, mediante señales disponibles, categorías aproximadas:

```text
DIGITAL
SCANNED
IMAGE
HANDWRITTEN_LIKELY
MIXED
```

La clasificación puede ser inicialmente heurística.

Su función no es decidir por sí sola el backend, sino aportar información al quality gate.

Ejemplo:

```text
DIGITAL + Docling OK
    → aceptar

SCANNED + extracción pobre
    → Vision OCR

HANDWRITTEN_LIKELY
    → Vision OCR

MIXED + texto parcial
    → evaluar calidad
```

---

# 9. Fase 4 — `VisionOCRBackend`

Crear una abstracción específica para recuperación visual.

Responsabilidades:

1. recibir una página;
2. obtener una imagen adecuada;
3. ejecutar el modelo visual;
4. recuperar texto;
5. normalizar el resultado;
6. devolver `PageExtractionResult`;
7. informar de errores y metadata.

Debe ser independiente del resto del pipeline.

---

# 10. Fase 5 — Integración de MiniCPM-V 4.5

MiniCPM-V 4.5 será la primera implementación del backend visual.

## Objetivo

Utilizarlo principalmente para:

- manuscritos;
- páginas escaneadas difíciles;
- fotografías;
- documentos donde Docling/OCR convencional no recupera suficiente información;
- páginas mixtas donde la información relevante es visual.

## Flujo

```text
PDF page
   ↓
render
   ↓
image
   ↓
MiniCPM-V 4.5
   ↓
text
   ↓
normalización
   ↓
PageExtractionResult
```

## Prompting

El prompt debe ser específico para extracción/transcripción documental.

No se debe pedir al modelo:

- análisis;
- interpretación;
- extracción de entidades;
- resumen;
- inferencias.

El objetivo de esta fase es recuperar fielmente el contenido visible.

### Principio

```text
Vision model = recuperación
LLM downstream = interpretación
```

No mezclar ambas responsabilidades.

---

# 11. Fase 6 — LightOnOCR

LightOnOCR se implementará como segundo backend cuando MiniCPM esté integrado y medido.

Objetivo:

- disponer de una alternativa especializada;
- comparar calidad;
- comparar latencia;
- comparar consumo de GPU;
- permitir cambiar de backend mediante configuración.

No debe bloquear la primera entrega funcional.

La interfaz debe permitir añadirlo posteriormente sin modificar el pipeline.

---

# 12. Fase 7 — Renderizado de páginas

El backend visual debe recibir una representación de imagen adecuada.

Definir:

- DPI/resolución inicial;
- formato;
- compresión;
- orientación;
- rotación;
- límites máximos de tamaño;
- estrategia para imágenes ya existentes en el PDF.

La resolución debe formar parte del benchmark.

No fijar prematuramente una resolución óptima.

Se deben comparar al menos:

```text
resolución baja
resolución media
resolución alta
```

especialmente para manuscritos.

---

# 13. Fase 8 — Document Assembly

Después de procesar todas las páginas:

```text
page 1 → Docling
page 2 → Docling
page 3 → MiniCPM
page 4 → Docling
...
```

se debe reconstruir un único documento canónico.

El orden de páginas debe conservarse exactamente.

La metadata de backend debe mantenerse internamente, pero no debe contaminar el texto destinado al extractor de entidades.

---

# 14. Fase 9 — Metadata y trazabilidad

Cada página procesada debe conservar información suficiente para explicar cómo se obtuvo el resultado.

Mínimo conceptual:

```json
{
  "page": 3,
  "backend": "minicpm-v4.5",
  "status": "fallback",
  "reason": "low_quality_docling"
}
```

Los nombres y estructura definitivos deben adaptarse al modelo existente.

## Posibles motivos

```text
LOW_TEXT_DENSITY
EMPTY_EXTRACTION
CORRUPTED_TEXT
IMAGE_PAGE
HANDWRITTEN_LIKELY
QUALITY_GATE
MANUAL_REPROCESS
```

No es necesario implementar todos inicialmente.

---

# 15. Fase 10 — Benchmark

Esta fase es obligatoria antes de decidir los valores definitivos del quality gate.

## Corpus mínimo

Crear un corpus representativo con:

- PDF digital limpio;
- PDF con layout complejo;
- PDF escaneado;
- PDF compuesto por imágenes;
- manuscrito limpio;
- manuscrito difícil;
- fotografías;
- documentos mixtos;
- tablas;
- páginas inclinadas/deformadas;
- documentos largos.

El corpus debe incluir documentos reales o representativos de los que textFlow pueda recibir en producción.

## Comparaciones

Como mínimo:

```text
Docling
Docling + MiniCPM fallback
MiniCPM completo
Docling + LightOnOCR fallback
```

Cuando LightOnOCR esté integrado.

---

# 16. Métricas de calidad

## Extracción

Medir:

- cobertura;
- caracteres correctos;
- palabras correctas;
- omisiones;
- caracteres erróneos;
- texto corrupto;
- duplicaciones;
- páginas recuperadas;
- páginas fallidas.

Cuando sea viable, utilizar métricas estándar de OCR como CER/WER sobre un subconjunto con ground truth.

## Calidad downstream

Medir específicamente:

- entidades correctamente recuperadas;
- entidades perdidas;
- entidades espurias;
- información relevante recuperada;
- calidad de las relaciones/estructuras que dependan del texto.

Esta métrica tiene prioridad sobre el simple número de caracteres.

---

# 17. Métricas de rendimiento

Registrar:

- tiempo por página;
- tiempo por documento;
- throughput;
- VRAM;
- RAM;
- CPU;
- tamaño de imagen;
- tokens generados cuando estén disponibles;
- concurrencia;
- tiempo de inicialización del modelo.

Separar:

```text
cold start
warm inference
```

porque el comportamiento operacional será diferente.

---

# 18. Criterio de selección del backend

No establecer un ganador únicamente por calidad OCR.

La decisión debe considerar conjuntamente:

```text
calidad
+
calidad downstream
+
latencia
+
throughput
+
VRAM
+
estabilidad
```

El objetivo no es que MiniCPM procese mejor absolutamente todo.

El objetivo es:

```text
usar extracción barata/rápida cuando funciona
y visión cuando realmente aporta valor
```

---

# 19. Quality Gate inicial

Después del benchmark se establecerán umbrales concretos.

Conceptualmente:

```text
Docling
   │
   ▼
Quality Gate
   │
   ├── PASS ──────────────► aceptar
   │
   ├── SUSPECT ───────────► MiniCPM
   │
   └── FAIL ──────────────► MiniCPM
```

Los umbrales no deben fijarse arbitrariamente antes del benchmark.

Deben derivarse de:

- corpus;
- falsos negativos;
- falsos positivos;
- coste de procesar visualmente una página.

---

# 20. Estrategia de fallback

La primera versión debe ser conservadora.

Si la extracción convencional es claramente mala:

```text
Docling → MiniCPM
```

Si es dudosa:

```text
Docling → MiniCPM
```

Si es buena:

```text
Docling → aceptar
```

No se recomienda inicialmente combinar ambos textos automáticamente.

Ejemplo no deseado:

```text
Docling text
+
MiniCPM text
=
merged text
```

Esto puede producir duplicaciones.

La primera implementación debe tratar MiniCPM como **reemplazo del resultado de la página** cuando el quality gate determine que el resultado convencional no es suficiente.

Más adelante podría estudiarse una estrategia de fusión si los datos lo justifican.

---

# 21. Gestión de errores

Si MiniCPM falla:

```text
Docling
   ↓
quality fail
   ↓
MiniCPM error
```

el sistema debe:

1. registrar el error;
2. conservar el resultado convencional si existe;
3. marcar la página como degradada;
4. permitir que el pipeline continúe cuando el contrato actual lo permita.

No debe provocar una caída global por un fallo de inferencia de una página salvo que la política actual del sistema exija documento completo válido.

Esta decisión debe confirmarse contra el comportamiento actual del `extraction-worker`.

---

# 22. Observabilidad

Añadir métricas operativas como:

```text
pages_processed_total
pages_docling_total
pages_vision_total
pages_fallback_total
vision_failures_total
quality_gate_pass_total
quality_gate_suspect_total
quality_gate_fail_total
```

Y, si la infraestructura actual lo permite:

```text
vision_processing_seconds
docling_processing_seconds
vision_vram_usage
```

Esto permitirá detectar rápidamente si el fallback se está activando demasiado.

---

# 23. Configuración

Los parámetros deben ser configurables sin modificar código.

Conceptualmente:

```text
VISION_OCR_ENABLED
VISION_OCR_BACKEND
VISION_OCR_MODEL
VISION_OCR_MAX_CONCURRENCY
VISION_OCR_IMAGE_DPI
VISION_OCR_TIMEOUT
VISION_OCR_MIN_TEXT_LENGTH
VISION_OCR_QUALITY_THRESHOLD
```

Los nombres definitivos deben seguir las convenciones existentes del proyecto.

Debe existir una forma sencilla de:

```text
activar/desactivar Vision OCR
cambiar backend
cambiar modelo
ajustar concurrencia
```

---

# 24. Despliegue

MiniCPM debe ejecutarse como componente independiente o mediante el mecanismo de serving ya utilizado por la plataforma, según lo que resulte más coherente con la infraestructura actual.

La primera implementación debe evitar acoplar el `extraction-worker` al runtime concreto del modelo.

Conceptualmente:

```text
extraction-worker
       │
       ▼
Vision OCR interface
       │
       ▼
model serving
       │
       ▼
MiniCPM-V 4.5
```

Esto permite sustituir el modelo sin modificar la lógica de extracción.

---

# 25. Fases de implementación

## Fase A — inspección

- revisar código actual;
- identificar contratos;
- localizar puntos de integración;
- confirmar modelo de datos.

**Salida:** diseño de integración concreto.

## Fase B — abstracción

- crear `ExtractionBackend`;
- crear resultado canónico de página si no existe;
- adaptar Docling.

**Salida:** Docling sigue funcionando exactamente igual.

## Fase C — Quality Gate

- implementar señales;
- introducir `PASS/SUSPECT/FAIL`;
- métricas;
- logging.

**Salida:** el sistema puede identificar páginas problemáticas sin todavía usar Vision OCR.

## Fase D — Vision OCR

- crear `VisionOCRBackend`;
- renderizado;
- integración MiniCPM;
- normalización;
- gestión de errores.

**Salida:** una página problemática puede recuperarse mediante MiniCPM.

## Fase E — Assembly

- combinar páginas;
- preservar orden;
- preservar metadata;
- producir salida canónica.

**Salida:** el pipeline downstream funciona sin cambios.

## Fase F — Benchmark

- corpus;
- ground truth;
- comparativas;
- métricas.

**Salida:** valores reales para decidir quality gate y configuración.

## Fase G — LightOnOCR

- implementar backend;
- repetir benchmark;
- comparar resultados.

**Salida:** segundo backend intercambiable.

## Fase H — endurecimiento

- observabilidad;
- timeouts;
- límites;
- concurrencia;
- recuperación de errores;
- configuración;
- documentación.

---

# 26. Criterios de aceptación

La implementación se considerará correcta cuando:

### Arquitectura

- [ ] El pipeline posterior no necesita conocer MiniCPM.
- [ ] El backend visual está desacoplado del runtime del modelo.
- [ ] El procesamiento se realiza por página.
- [ ] Docling continúa funcionando sin regresiones.

### Calidad

- [ ] Los manuscritos del corpus recuperan texto utilizable.
- [ ] Las páginas que Docling procesa correctamente no se envían innecesariamente a Vision OCR.
- [ ] El quality gate detecta una proporción suficiente de fallos reales.
- [ ] Se mide calidad downstream.

### Operación

- [ ] Se puede activar/desactivar Vision OCR mediante configuración.
- [ ] Los fallos del modelo quedan registrados.
- [ ] Se dispone de métricas de utilización.
- [ ] Se conserva la procedencia por página.
- [ ] Existe control de concurrencia y timeout.

### Rendimiento

- [ ] Se conocen latencia y throughput de MiniCPM.
- [ ] Se conoce consumo de VRAM.
- [ ] Se ha medido el impacto sobre documentos largos.

---

# 27. Decisiones deliberadamente pendientes

Estas decisiones **no deben cerrarse antes del benchmark**:

1. Umbral exacto del quality gate.
2. Resolución/DPI óptimo.
3. Concurrencia de MiniCPM.
4. Prompt definitivo.
5. Si determinadas páginas deben pasar directamente a Vision OCR por tipo.
6. Necesidad de LightOnOCR en producción.
7. Si merece la pena procesar determinados documentos completos mediante Vision OCR.
8. Necesidad de un clasificador más sofisticado.
9. Política exacta cuando Vision OCR falla.

---

# 28. Resultado final esperado

El comportamiento final debe ser transparente para el resto de textFlow:

```text
                    ┌── Docling ────────────────┐
                    │                            │
Documento ─► páginas ┤                            ├──► documento canónico
                    │                            │
                    └── MiniCPM si es necesario ─┘
                                                   │
                                                   ▼
                                           pipeline existente
```

Un documento manuscrito deja de ser un caso especial para el resto de la plataforma.

La complejidad queda encapsulada dentro de `extraction-worker`.

---

# 29. Resumen ejecutivo

La modificación propuesta **no requiere rehacer la arquitectura de textFlow**.

La evolución consiste en convertir `extraction-worker` en un sistema de extracción híbrido capaz de elegir el mecanismo adecuado por página:

```text
Docling
   ↓
Quality Gate
   ├── PASS → aceptar
   └── SUSPECT/FAIL → MiniCPM-V 4.5
                         ↓
                    texto canónico
                         ↓
                   pipeline actual
```

MiniCPM-V 4.5 será el primer backend visual por su comportamiento observado con documentos manuscritos.

LightOnOCR queda preparado como alternativa.

La prioridad de implementación debe ser:

```text
1. contrato
2. quality gate
3. MiniCPM
4. assembly
5. benchmark
6. LightOnOCR
7. optimización
```

El principio fundamental es mantener una única salida documental canónica. La incorporación de visión debe mejorar la recuperación del texto, no crear un segundo pipeline paralelo.

---

## Referencia al diseño previo

Este plan desarrolla el enfoque híbrido previamente planteado para `extraction-worker`: mantener la extracción convencional como primera ruta, evaluar la calidad y utilizar visión únicamente cuando sea necesario, preferentemente a nivel de página. La arquitectura previa también establecía que el resultado visual debía integrarse en el mismo texto normalizado que consume el resto de textFlow.
