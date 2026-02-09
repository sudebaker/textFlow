# Plan: Investigación de Soluciones NER (Sin Truncamiento)

**Fecha**: 2026-02-08  
**Decisiones del Usuario**:
1. ✅ Opción B: Investigar Unstructured API para NER
2. ✅ GPU: Procesaremos con GPU (tiempos +70% negligibles)
3. ✅ Opción C: Estrategia en fases
4. ✅ Alternativa: Investigar modelo NER sin límite 384 tokens

**Status**: PLAN (research phase)

---

## 📊 Decisiones y Rationale

### Decisión 1: Investigar Unstructured API para NER ✅

**Por qué**: 
- Mejor manejo de contexto global (sin truncamiento artificial)
- Una pasada unificada: extracción + NER
- Unstructured ya integrado en pipeline

**Impacto esperado**:
- 0% pérdida por truncamiento (vs 25% actual)
- Potencial mejora de calidad (modelos entrenados por Unstructured)
- Menor complejidad que GLiNER local

### Decisión 2: Usar GPU ✅

**Por qué**:
- Tiempos +70% (16 min vs 9.5 min) son NEGLIGIBLES en GPU
- Embeddings-worker ya usa GPU con fallback CPU
- Performance no será constraint

**Impacto esperado**:
- Overhead de 300 tokens: ~1.5s → ~0.05s con GPU
- Total: mismo tiempo o más rápido

### Decisión 3: Opción C (Fases) ✅

**Por qué**:
- Reduce riesgo
- Permite validación en cada fase
- Decisiones data-driven

**Fases**:
1. Investigar Unstructured API NER disponibilidad
2. Si disponible: prototipar
3. Si no: investigar modelo NER alternativo

### Decisión 4: Modelo NER Alternativo ✅

**Por qué**:
- GLiNER tiene límite artificial (384 tokens)
- Otros modelos pueden manejar secuencias más largas
- Alternativa si Unstructured no expone NER

**Candidatos a investigar**:
- spaCy (open source, sin límite, buena calidad)
- mBERT NER (multilingual, flexible)
- RoBERTa-based NER (más moderno)
- LLMs pequeños (Mistral 7B fine-tuned)

---

## 🔍 FASE 1: Investigación Unstructured API NER

### Objetivo
Determinar si Unstructured API pública expone funcionalidad de extracción de entidades

### Tareas

#### Tarea 1.1: Explorar API Endpoints
```
1. Revisar OpenAPI spec completo de Unstructured
2. Buscar endpoints con "entity", "ner", "extract", "enrichment"
3. Revisar parámetros disponibles en /general/v0/general
4. Verificar si hay "strategy" para NER
```

#### Tarea 1.2: Revisar Documentación Oficial
```
Buscar en https://docs.unstructured.io/:
- NER capabilities
- Entity extraction
- Enrichment features
- Custom models support
```

#### Tarea 1.3: Test Práctico
```
Enviar request a Unstructured API:
POST /general/v0/general
- Probar parámetro "strategy" con valores NER-like
- Probar parametrización para extracción de entidades
- Analizar response para campos de entidades
```

#### Tarea 1.4: Documentar Hallazgos
```
Resumen con:
- ¿Está disponible NER? (SÍ/NO/PARCIAL)
- Si SÍ: Qué tipos de entidades soporta
- Limitaciones conocidas
- Modelos subyacentes
```

### Resultado Esperado

**Si SÍ está disponible**:
```
→ Proceder a Fase 2: Prototipo Unstructured NER
```

**Si NO está disponible**:
```
→ Proceder a Fase 3: Investigar modelos NER alternativos
```

---

## 🔌 FASE 2: Prototipo Unstructured API NER (Si disponible)

### Objetivo
Implementar un worker que use Unstructured para NER en lugar de GLiNER

### Diseño Propuesto

```python
# Pseudocódigo: entities-worker-unstructured.py

class UnstructuredEntitiesWorker:
    def __init__(self):
        self.unstructured_url = "http://unstructured:8000"
    
    def extract_entities(self, text: str) -> List[Entity]:
        """
        Envía texto a Unstructured API para NER.
        Una pasada → obtiene entidades con contexto global
        """
        response = requests.post(
            f"{self.unstructured_url}/general/v0/general",
            json={
                "text": text,
                "strategy": "ner"  # O similar
            }
        )
        entities = self.parse_response(response)
        return entities
```

### Ventajas vs GLiNER

| Aspecto | GLiNER (actual) | Unstructured |
|--------|-----------------|-------------|
| **Truncamiento** | 384 tokens | Global (sin límite) |
| **Contexto** | Por chunk | Por documento |
| **Qualidad** | ~1590 entities | Esperado similar+ |
| **Overhead** | Alto (per-chunk) | Bajo (por documento) |
| **Control** | Alto (thresholds) | Bajo (menos params) |

### Métricas de Éxito

```
1. Entity count: >= 1590 raw entities
2. Distribution: Similar a GLiNER (PER 40-50%, ORG 20-25%, etc)
3. Time: Comparable con GPU (< 30 segundos)
4. Quality: Manual review de 10 chunks (sin falsos positivos obvios)
```

---

## 🤖 FASE 3: Investigar Modelos NER Alternativos

### Objetivo
Si Unstructured no expone NER, encontrar modelo alternativo a GLiNER sin límite 384 tokens

### Candidatos a Evaluar

#### Candidato 1: spaCy + Custom Model
```
Pro:
- ✅ Open source
- ✅ Sin límite de tokens (procesa todo)
- ✅ Bien documentado
- ✅ Trained models españoles disponibles

Contra:
- ⚠️ Calidad inferior a GLiNER (según benchmarks)
- ⚠️ Requiere fine-tuning para dominio legal

Complejidad: MEDIA
```

#### Candidato 2: mBERT (Multilingual BERT) + Fine-tuned
```
Pro:
- ✅ Multilingual (español OK)
- ✅ BERT moderno (mejor que RNN)
- ✅ Sin límite inherente

Contra:
- ⚠️ Requiere training (no preentrenado para NER)
- ⚠️ Latencia BERT (más lento que spaCy)

Complejidad: ALTA
```

#### Candidato 3: RoBERTa-based NER
```
Pro:
- ✅ RoBERTa es más poderoso que BERT
- ✅ Modelos españoles disponibles
- ✅ Sin límite de tokens

Contra:
- ⚠️ Latencia media-alta
- ⚠️ Requiere tuning para dominio legal

Complejidad: ALTA
```

#### Candidato 4: LLMs Pequeños (Mistral 7B)
```
Pro:
- ✅ Contexto global (puede procesar 32K tokens+)
- ✅ Fine-tuning posible
- ✅ Potencial mejor calidad

Contra:
- ❌ Muy lento (no viable sin GPU potente)
- ❌ Overhead computacional no justificado

Complejidad: MUY ALTA
```

### Evaluación

| Modelo | Calidad | Speed | Complejidad | Viabilidad |
|--------|---------|-------|------------|-----------|
| spaCy | MEDIA | RÁPIDO | MEDIA | ✅ VIABLE |
| mBERT | MEDIA-ALTA | LENTO | ALTA | ⚠️ |
| RoBERTa | ALTA | LENTO | ALTA | ⚠️ |
| Mistral | MUY ALTA | MUY LENTO | MUY ALTA | ❌ |

**Recomendación**: Empezar con **spaCy** (mejor ratio viabilidad/calidad)

---

## 📋 Plan de Investigación Específico

### FASE 1: Investigar Unstructured (1-2 horas)

```
Tareas en paralelo:
1. Revisar OpenAPI spec (buscar "entity", "ner", "enrichment")
2. Revisar docs (https://docs.unstructured.io/)
3. Test práctico: enviar request a API local
4. Documentar hallazgos

Decisión gate:
- Si SÍ tiene NER → FASE 2 (prototipo)
- Si NO tiene NER → FASE 3 (spaCy)
```

### FASE 2: Prototipo Unstructured NER (Si aplica - 4-6 horas)

```
1. Diseñar worker que use Unstructured NER
2. Implementar extracción de entidades
3. Test con documento real (sentencia-fiscal)
4. Comparar: GLiNER vs Unstructured
   - Entity count
   - Distribution
   - Time
   - Quality (manual review)
5. Decisión: adoptar o rechazar
```

### FASE 3: Alternativa spaCy (Si FASE 2 falla - 6-8 horas)

```
1. Instalar spaCy + spanish models
2. Comparar spaCy vs GLiNER
   - Out-of-the-box quality
   - Sin truncamiento (spaCy procesa todo)
3. Si calidad es aceptable: usar spaCy
4. Si no: considerar fine-tuning
```

---

## ⚙️ Cambios de Configuración (GPU)

### Ya hecho ✅
```
embeddings-worker:
- ✅ detect_gpu() function
- ✅ Fallback a CPU
- ✅ CUDA_VISIBLE_DEVICES=0
```

### TODO para entities-worker
```
1. Agregar CUDA_VISIBLE_DEVICES en docker-compose.yml
2. Verificar torch.cuda.is_available() en detect_gpu()
3. Confirmar device mapping en load_model()
```

### Impacto esperado con GPU
```
GLiNER (CPU): 574 segundos / 296 chunks = 1.94 s/chunk
GLiNER (GPU): ~0.05-0.1 s/chunk = 15-30 segundos total

Opción B (Unstructured, GPU): Probablemente similar o más rápido
Opción C (spaCy, GPU): Más rápido que GLiNER
```

---

## 🎯 Decisión Tree

```
FASE 1: ¿Unstructured tiene NER?
    ↓
    ├─ SÍ → FASE 2: Prototipo Unstructured
    │        ├─ ¿Funciona bien?
    │        │   ├─ SÍ → ADOPTAR Unstructured NER
    │        │   └─ NO → FASE 3: spaCy
    │        └─
    │
    └─ NO → FASE 3: Investigar spaCy
             ├─ ¿Calidad suficiente?
             │   ├─ SÍ → ADOPTAR spaCy
             │   └─ NO → Fine-tune spaCy O considerar RoBERTa
             └─
```

---

## 📊 Recursos Necesarios

### Para Investigación (FASE 1)
```
- Acceso a Unstructured API docs
- API local corriendo (ya lo tienes)
- 1-2 horas
```

### Para Prototipo Unstructured (FASE 2, si aplica)
```
- Código Python: ~100 líneas
- Testing: documento real
- 4-6 horas
```

### Para spaCy Alternative (FASE 3, si aplica)
```
- spaCy instalado + modelos españoles
- Código Python: ~150 líneas
- Testing y potencial fine-tuning
- 6-8 horas
```

---

## 🎬 Próximos Pasos

### Ahora (FASE 1 - Investigación)

1. **Investigar Unstructured API NER**
   - Revisar OpenAPI spec completo
   - Buscar endpoints de entidades/enrichment
   - Test práctico

2. **Documentar hallazgos**
   - ¿Está disponible NER? (SÍ/NO/PARCIAL)
   - Si SÍ: qué soporta
   - Si NO: proceder a spaCy

### Después (FASE 2 o 3)

Basado en resultados de FASE 1:
- **Si Unstructured tiene NER**: Prototipo Unstructured
- **Si no**: Prototipo spaCy

### GPU

Confirmar GPU en entities-worker para Fase 2/3

---

## 📝 Notas Importantes

### Por qué Unstructured primero

```
1. Ya está integrado en el pipeline
2. Una pasada unificada (extracción + NER)
3. Mantiene contexto global
4. Si funciona: no hay refactor mayor
```

### Por qué spaCy como alternativa

```
1. Si Unstructured no tiene NER pública
2. Open source, sin límites artificiales
3. Balance calidad/complejidad
4. GPU-compatible con transformers backend
```

### Por qué NO LLMs grandes

```
1. Mistral 7B: 0.5+ segundos/documento (demasiado lento incluso con GPU)
2. Overhead computacional no justificado
3. Mejor resultado con modelos específicos (spaCy/BERT)
```

---

## ✅ Success Criteria

### FASE 1 (Investigación)
- [ ] OpenAPI spec revisado
- [ ] Docs exploradas completamente
- [ ] Test práctico realizado
- [ ] Documentación de hallazgos

### FASE 2 o 3 (Prototipo)
- [ ] Código implementado
- [ ] Entity count >= 1590
- [ ] Distribution similar a GLiNER
- [ ] Manual review OK (no falsos positivos obvios)
- [ ] Time < 30 segundos (con GPU)

### Decisión Final
- [ ] Adoptar Unstructured NER O spaCy
- [ ] Reemplazar GLiNER
- [ ] Producción ready