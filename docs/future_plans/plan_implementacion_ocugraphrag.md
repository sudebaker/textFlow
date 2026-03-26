# Plan de Mejora: UCO GraphRAG — Integración de Capacidades Avanzadas

**Fecha:** 2026-03-26  
**Destinatario:** Agente de implementación  
**Repositorio objetivo:** `git@github.com:sudebaker/ocugraphrag.git`  
**Rama de trabajo recomendada:** `feature/inference-pipeline`

---

## Contexto

UCO GraphRAG es una plataforma de análisis de inteligencia basada en RAG híbrido (Memgraph + Qdrant). Tiene un pipeline de ingesta sólido, un agente autónomo de hipótesis (`AutonomousHypothesisInvestigator`) y un sistema de citaciones. Sin embargo, carece de las siguientes capacidades que este plan describe cómo implementar desde cero.

---

## MEJORA 1: Pipeline de Inferencias Jerarquizadas (Micro → Meso → Macro)

### Qué es y por qué

Actualmente el sistema extrae entidades y relaciones de los documentos, pero no genera **productos de inteligencia derivados**. Una inferencia es una conclusión factual extraída del texto que va más allá de la entidad pura. Hay tres niveles:

- **Micro-inferencia:** hecho concreto extraído de un fragmento de texto. Ej: "Juan Pérez firmó escritura ante notario el 15/03/2020 transfiriendo inmueble valorado en 450.000€"
- **Meso-inferencia:** patrón o conexión entre varias micros del mismo episodio. Ej: "Juan Pérez realizó 3 transferencias inmobiliarias en 18 meses por valor total de 1.2M€"
- **Macro-inferencia / hipótesis:** conclusión de alto nivel sobre el caso. Ej: "Posible blanqueo de capitales mediante fraccionamiento de transmisiones inmobiliarias"

### Arquitectura a implementar

#### 1.1 Nuevo modelo de datos en Memgraph

Añadir tres nuevos tipos de nodo al schema:

```cypher
-- Micro-inferencia
CREATE (m:MicroInference {
    id: "uuid",
    text: "texto de la inferencia",
    chunk_id: "origen chunk",
    document_id: "origen documento",
    collection_name: "colección",
    entities: ["lista", "de", "entidades"],
    source_type: "catastro|notariado|registro|generico",
    confidence: 0.85,
    created_at: datetime()
})

-- Meso-inferencia
CREATE (ms:MesoInference {
    id: "uuid",
    text: "patrón o conexión detectada",
    episode_id: "id del episodio al que pertenece",
    micro_ids: ["lista de micros que la generaron"],
    confidence: 0.75,
    created_at: datetime()
})

-- Macro-inferencia/Hipótesis enriquecida
CREATE (ma:MacroInference {
    id: "uuid",
    text: "hipótesis de alto nivel",
    meso_ids: ["lista de mesos que la generaron"],
    case_id: "caso",
    confidence: 0.70,
    created_at: datetime()
})
```

Añadir relaciones:
```cypher
(m:MicroInference)-[:SUPPORTS]->(ms:MesoInference)
(ms:MesoInference)-[:SUPPORTS]->(ma:MacroInference)
(m:MicroInference)-[:DERIVED_FROM]->(c:Chunk)
```

#### 1.2 Nuevo servicio: `InferenceExtractionService`

**Fichero:** `services/inference_extraction_service.py`

Este servicio se encarga de generar micro-inferencias a partir de chunks ya procesados. Se ejecuta en el worker GPU (`ai_worker`) justo después de la extracción de entidades actual.

```python
class InferenceExtractionService:
    """
    Genera micro-inferencias a partir de chunks de texto y sus entidades.
    
    Se integra en el pipeline de ingesta existente, después de la 
    extracción de entidades (GlinearClient).
    """
    
    async def extract_micro_inferences(
        self,
        chunk_text: str,
        chunk_id: str,
        document_id: str,
        collection_name: str,
        entities: List[Dict],  # salida de GlinearClient
        source_type: str = "generico"
    ) -> List[MicroInference]:
        """
        Usa el LLM para extraer hechos concretos del chunk,
        guiado por las entidades ya detectadas.
        
        Prompt orientativo (adaptar al LLM en uso):
        - "Dado este texto y estas entidades detectadas, extrae todos los
          hechos concretos y verificables en formato afirmación directa.
          Cada hecho debe mencionar al menos una entidad. Máximo 8 hechos."
        """
```

**Integración en el pipeline existente:**

En `tasks/ai_tasks.py` (o donde se llame actualmente a `GlinearClient`), añadir después de la extracción de entidades:

```python
# EXISTENTE:
entities = await glinear_client.extract_entities(chunk_text, labels)

# NUEVO (añadir a continuación):
micro_inferences = await inference_service.extract_micro_inferences(
    chunk_text=chunk_text,
    chunk_id=chunk_id,
    document_id=document_id,
    collection_name=collection_name,
    entities=entities,
    source_type=detect_source_type(document_metadata)  # ver sección 3
)
await graph_repo.save_micro_inferences(micro_inferences)
await vector_repo.embed_micro_inferences(micro_inferences)  # vectorizar también
```

#### 1.3 Nuevo servicio: `InferenceSynthesisService`

**Fichero:** `services/inference_synthesis_service.py`

Servicio asíncrono (Celery task) que, tras completar la ingesta de un conjunto de documentos de un caso, genera mesos y macros a partir de las micros. **No bloquea la ingesta.**

```python
class InferenceSynthesisService:
    
    async def generate_meso_inferences(
        self,
        collection_name: str,
        episode_id: str,
        micro_inferences: List[MicroInference]
    ) -> List[MesoInference]:
        """
        A partir de un grupo de micros (episodio), detecta patrones.
        Usa el LLM principal (no el NER pequeño).
        """
    
    async def generate_macro_inferences(
        self, 
        collection_name: str,
        meso_inferences: List[MesoInference]
    ) -> List[MacroInference]:
        """
        A partir de los patrones detectados, genera hipótesis de alto nivel.
        Estas hipótesis deben alimentar al AutonomousHypothesisInvestigator
        existente como input adicional (no reemplazarlo).
        """
```

**Integración con el agente autónomo existente:**

En `services/autonomous_hypothesis_investigator.py`, en el método que genera las hipótesis iniciales, añadir como fuente adicional las `MacroInference` almacenadas en Memgraph:

```python
# Fuente 1 (existente): HypothesisService basado en patrones de grafo
existing_hypotheses = await hypothesis_service.generate_hypotheses(collection)

# Fuente 2 (NUEVA): MacroInferences del pipeline de inferencias
macro_inferences = await graph_repo.get_macro_inferences(collection_name=collection)
inferred_hypotheses = self._convert_macros_to_hypotheses(macro_inferences)

# Combinar y deduplicar
all_hypotheses = self._merge_hypotheses(existing_hypotheses, inferred_hypotheses)
```

---

## MEJORA 2: Clustering Episódico

### Qué es y por qué

Sin esta mejora, el agente trabaja sobre miles de micro-inferencias sueltas. El clustering las agrupa en "episodios" — conjuntos coherentes de hechos relacionados — antes de sintetizar patrones. Esto reduce el ruido y mejora enormemente la calidad de las meso-inferencias.

### Arquitectura

#### 2.1 Nuevo modelo de datos

```cypher
CREATE (e:Episode {
    id: "uuid",
    collection_name: "colección",
    case_id: "caso",
    size: 14,  -- número de micros que contiene
    summary: "resumen LLM del episodio",
    created_at: datetime()
})

(m:MicroInference)-[:BELONGS_TO]->(e:Episode)
```

#### 2.2 Nuevo servicio: `EpisodeClusteringService`

**Fichero:** `services/episode_clustering_service.py`

Este servicio implementa un grafo de conectividad ponderado con tres canales para agrupar micro-inferencias en episodios. Se ejecuta como **Celery task asíncrona** después de que todas las micros de un caso estén generadas.

```python
class EpisodeClusteringService:
    """
    Agrupa micro-inferencias en episodios factuales coherentes.
    
    Usa tres canales de similitud ponderados:
    - Canal A (peso 0.5): Co-ocurrencia de entidades (Jaccard)
    - Canal B (peso 0.3): Proximidad en grafo Memgraph (1-2 hops)
    - Canal C (peso 0.2): Similitud semántica coseno (embeddings Qdrant)
    
    Algoritmo de clustering: Louvain sobre el grafo de conectividad.
    """
    
    def __init__(self):
        self.graph_service = get_graph_service()
        self.vector_service = get_vector_service()
        self.cache_service = get_cache_service()
        
        # Pesos de los canales
        self.WEIGHT_A = 0.5  # co-ocurrencia entidades
        self.WEIGHT_B = 0.3  # proximidad grafo
        self.WEIGHT_C = 0.2  # similitud semántica
        self.EDGE_THRESHOLD = 0.15  # umbral mínimo para crear arista
        self.LOUVAIN_RESOLUTION = 2.5

    async def cluster_collection(
        self, 
        collection_name: str
    ) -> List[Episode]:
        """
        Entry point principal. Agrupa todas las micros de una colección.
        
        Pasos:
        1. Cargar todas las MicroInference de la colección desde Memgraph
        2. Construir grafo de conectividad (canales A+B+C)
        3. Ejecutar Louvain
        4. Generar episodios y persistirlos
        5. Para cada episodio, generar summary con LLM
        """
    
    async def _build_canal_a(
        self, 
        micros: List[MicroInference]
    ) -> Dict[Tuple[str,str], float]:
        """
        Canal A: Índice Jaccard entre conjuntos de entidades de cada par.
        
        jaccard(A, B) = |A ∩ B| / |A ∪ B|
        
        Solo crear arista si jaccard > 0.1 (al menos 1 entidad en común).
        """
    
    async def _build_canal_b(
        self, 
        micros: List[MicroInference]
    ) -> Dict[Tuple[str,str], float]:
        """
        Canal B: Proximidad en grafo Memgraph a 1-2 hops.
        
        Query Cypher orientativa:
        MATCH (m1:MicroInference {id: $id1})-[:MENTIONS]->(e:Entity)
              <-[:MENTIONS]-(m2:MicroInference {id: $id2})
        RETURN count(e) as shared_entities
        
        O buscar si las entidades de m1 y m2 están conectadas 
        a 1-2 hops en el grafo de relaciones.
        Peso = 1.0 si hay conexión directa, 0.5 si a 2 hops.
        """
    
    async def _build_canal_c(
        self,
        micros: List[MicroInference]
    ) -> Dict[Tuple[str,str], float]:
        """
        Canal C: Similitud coseno entre embeddings de las micros.
        
        Los embeddings de las micros se generan al guardarlas en Qdrant
        (ver InferenceExtractionService). Aquí se recuperan y se hace 
        búsqueda KNN (top-10 vecinos más cercanos por micro).
        
        Solo crear arista si coseno > 0.75.
        Usar el VectorService.search() existente con 
        collection="micro_inferences_{collection_name}".
        """
    
    def _run_louvain(
        self, 
        edges: Dict[Tuple[str,str], float],
        node_ids: List[str]
    ) -> Dict[str, int]:
        """
        Ejecutar Louvain sobre el grafo de conectividad.
        
        Dependencia a añadir: python-louvain (community)
        pip install python-louvain networkx
        
        Retorna: {micro_id: community_id}
        """
        import networkx as nx
        import community as community_louvain
        
        G = nx.Graph()
        G.add_nodes_from(node_ids)
        for (n1, n2), weight in edges.items():
            if weight >= self.EDGE_THRESHOLD:
                G.add_edge(n1, n2, weight=weight)
        
        partition = community_louvain.best_partition(
            G, 
            resolution=self.LOUVAIN_RESOLUTION,
            weight='weight'
        )
        return partition
```

#### 2.3 Dependencias a añadir

En `requirements.txt` o `pyproject.toml`:
```
python-louvain>=0.16
networkx>=3.0
```

#### 2.4 Celery task a crear

En `tasks/analysis_tasks.py` (crear si no existe):

```python
@celery_app.task(name="tasks.cluster_episodes")
async def cluster_episodes_task(collection_name: str):
    """
    Task asíncrona que agrupa micro-inferencias en episodios.
    Se lanza automáticamente al completar la ingesta de documentos de un caso.
    También se puede lanzar manualmente via API.
    """
    service = EpisodeClusteringService()
    episodes = await service.cluster_collection(collection_name)
    
    # Tras clustering, lanzar síntesis de mesos
    synthesis_service = InferenceSynthesisService()
    for episode in episodes:
        await synthesis_service.generate_meso_inferences(
            collection_name=collection_name,
            episode_id=episode.id,
            micro_inferences=episode.micros
        )
```

#### 2.5 Endpoint API

En `routers/investigation/` (añadir a un router de investigación existente o crear):

```python
@router.post("/collections/{collection_name}/cluster")
async def trigger_clustering(collection_name: str):
    """Lanza clustering episódico manual sobre una colección."""
    task = cluster_episodes_task.delay(collection_name)
    return {"task_id": task.id, "status": "queued"}

@router.get("/collections/{collection_name}/episodes")  
async def get_episodes(collection_name: str):
    """Lista los episodios generados para una colección."""
```

---

## MEJORA 3: Metadata de Origen (Source Type) en Documentos y Citaciones

### Qué es y por qué

Actualmente el sistema cita chunks (`[Fuente 1]`) pero no indica el **tipo de fuente** (registro de propiedad, escritura notarial, informe mercantil, resolución judicial, etc.). Para investigaciones patrimoniales, el peso probatorio varía enormemente según el origen.

### Implementación

#### 3.1 Ampliar el schema de Document en Memgraph

```cypher
-- Añadir campo source_type al nodo Document/Chunk existente
SET document.source_type = "notariado"  -- catastro|notariado|registro|mercantil|judicial|bancario|email|generico
SET document.source_institution = "Notaría Pérez García - Madrid"
SET document.source_confidence = 0.95  -- nivel de confianza de la fuente
```

#### 3.2 Detección automática de tipo de fuente

**Fichero:** `services/source_classifier.py` (nuevo)

```python
class SourceClassifier:
    """
    Detecta el tipo de fuente de un documento basándose en 
    nombre de fichero, metadatos y contenido inicial.
    """
    
    # Patrones para detección por nombre/contenido
    SOURCE_PATTERNS = {
        "catastro": ["catastro", "nota simple catastral", "certificación catastral"],
        "notariado": ["escritura", "notario", "protocolo", "otorgante"],
        "registro": ["registro de la propiedad", "nota simple registral", "inscripción"],
        "mercantil": ["registro mercantil", "informe mercantil", "deposit"],
        "judicial": ["auto", "sentencia", "providencia", "juzgado", "tribunal"],
        "bancario": ["extracto", "norma 43", "swift", "iban", "transferencia"],
        "email": [".eml", ".msg", "from:", "subject:"],
    }
    
    def classify(self, filename: str, content_preview: str) -> str:
        """Retorna el source_type detectado o 'generico' si no se detecta."""
```

#### 3.3 Integración en el pipeline de ingesta

En `DocumentRouter` o en `unified_ingest_router.py`, al crear el documento:

```python
source_classifier = SourceClassifier()
source_type = source_classifier.classify(
    filename=file.filename,
    content_preview=extracted_text[:500]
)
document_metadata["source_type"] = source_type
```

#### 3.4 Mejorar citaciones en respuestas RAG

En `services/intelligence_service.py` o donde se construyen las respuestas con citaciones, cambiar el formato de `[Fuente N]` a `[Fuente N - Escritura Notarial]` o similar. Recuperar el `source_type` del chunk al construir el contexto RAG.

---

## MEJORA 4: Sistema de Informes en Dos Velocidades

### Qué es y por qué

El `IntelligenceService` actual genera informes de forma síncrona. Para un equipo investigador necesita dos modos:
- **Rápido (< 10s):** informe sobre un filtro específico (actores de un caso, transacciones sospechosas)
- **Profundo (30min-2h):** análisis completo del caso con todas las inferencias

### Implementación

#### 4.1 Refactorizar IntelligenceService

En `services/intelligence_service.py`, añadir dos métodos diferenciados:

```python
async def generate_quick_report(
    self,
    collection_name: str,
    report_type: str,  # "actors" | "timeline" | "contradictions" | "transactions"
    filters: Dict[str, Any] = None,
    max_items: int = 50
) -> Dict:
    """
    Informe rápido (<10s). 
    - Consulta Memgraph/Qdrant directamente con filtros específicos
    - No usa el agente autónomo
    - Cacheable en Redis (TTL: 1h)
    - Usa el LLM pequeño (vllm-ner) para narrativa
    """

async def schedule_deep_report(
    self,
    collection_name: str,
    case_id: str,
    sections: List[str] = None  # Si None, incluye todo
) -> str:
    """
    Programa análisis profundo asíncrono.
    - Retorna task_id inmediatamente
    - Ejecuta en Celery (cpu_worker o ai_worker según sección)
    - Incluye episodios, inferencias meso/macro, contradicciones, red de actores
    - Notifica por webhook/polling cuando termina
    - Usa el LLM principal (vllm) para narrativa
    """
```

#### 4.2 Celery task para análisis profundo

En `tasks/analysis_tasks.py`:

```python
@celery_app.task(name="tasks.deep_report", bind=True, max_retries=2)
async def generate_deep_report_task(
    self, 
    collection_name: str, 
    case_id: str,
    sections: List[str]
):
    """
    Genera análisis profundo completo. Puede tardar 30min-2h.
    Progreso disponible via GET /reports/tasks/{task_id}/status
    """
```

#### 4.3 Nuevos endpoints

En `routers/intelligence_router.py`:

```python
@router.post("/reports/quick")
async def quick_report(request: QuickReportRequest):
    """Informe rápido síncrono."""

@router.post("/reports/deep")  
async def deep_report(request: DeepReportRequest):
    """Programa análisis profundo. Retorna task_id."""

@router.get("/reports/tasks/{task_id}/status")
async def report_task_status(task_id: str):
    """Polling del estado de un análisis profundo."""
```

---

## MEJORA 5: Traducción Multilingüe (Opcional, baja prioridad)

### Qué es y por qué

Para casos con documentos en árabe, ruso, chino o rumano. Se implementa como microservicio independiente.

### Implementación

#### 5.1 Nuevo microservicio Docker

**Fichero:** `translation_service/main.py` + `translation_service/Dockerfile`

```python
# FastAPI independiente que expone:
# POST /translate  {"text": "...", "source_lang": "auto", "target_lang": "es"}
# GET /health

# Modelo: cualquier modelo de traducción multilingüe compatible con 
# la infraestructura existente (vLLM o Ollama).
# Alternativa ligera: Helsinki-NLP/opus-mt-* via HuggingFace
```

#### 5.2 En docker-compose.yml

```yaml
translation:
  build: ./translation_service
  environment:
    - MODEL_NAME=Helsinki-NLP/opus-mt-mul-es  # o el modelo elegido
  deploy:
    resources:
      reservations:
        devices:
          - capabilities: [gpu]
```

#### 5.3 Integración en DocumentRouter

```python
# En document_router.py, tras detectar idioma:
if detected_language != "es":
    translated_text = await translation_client.translate(
        text=extracted_text,
        source_lang=detected_language,
        target_lang="es"
    )
    # Guardar también el texto original para auditoría
```

---

## Orden de Implementación

| Fase | Mejora | Ficheros principales | Dependencias nuevas | Prioridad |
|------|--------|---------------------|--------------------|----|
| 1 | Source Classifier (Mejora 3) | `services/source_classifier.py` | Ninguna | Alta — base para todo |
| 2 | Micro-inferencias (Mejora 1.1-1.2) | `services/inference_extraction_service.py`, schema Memgraph | Ninguna | Alta |
| 3 | Clustering episódico (Mejora 2) | `services/episode_clustering_service.py`, `tasks/analysis_tasks.py` | `python-louvain`, `networkx` | Alta |
| 4 | Meso/Macro inferencias (Mejora 1.3) | `services/inference_synthesis_service.py` | Ninguna | Media |
| 5 | Informes dos velocidades (Mejora 4) | `services/intelligence_service.py`, `routers/intelligence_router.py` | Ninguna | Media |
| 6 | Citaciones enriquecidas (Mejora 3.4) | `services/intelligence_service.py` | Ninguna | Baja |
| 7 | Traducción (Mejora 5) | `translation_service/` nuevo, `docker-compose.yml` | Modelo traducción | Opcional |

---

## Notas para el agente implementador

1. **No romper lo existente.** Todas las mejoras son aditivas. El pipeline de ingesta actual no se modifica, solo se extiende.

2. **Schema Memgraph:** Antes de crear nodos nuevos, revisar `repositories/graph_repository.py` para entender el schema actual y las convenciones de nomenclatura usadas.

3. **Celery workers:** El proyecto ya tiene `ai_worker` (GPU) y `cpu_worker`. Las tareas de embedding y extracción LLM van al `ai_worker`; el clustering (networkx/Louvain) puede ir al `cpu_worker`.

4. **Caché:** Usar siempre `get_cache_service()` existente para resultados intermedios costosos (embeddings de micros ya calculados, episodios ya clusterizados).

5. **Tests:** El proyecto tiene tests en `tests/`. Añadir al menos tests unitarios para `SourceClassifier` y `EpisodeClusteringService._run_louvain()`.

6. **Config:** Añadir los nuevos parámetros (pesos de canales, threshold Louvain, etc.) a `settings.py` como constantes con valores por defecto sensatos. No hardcodear en los servicios.

7. **Logging:** Usar `get_logger(__name__)` (patrón existente en el proyecto). Los tiempos de procesamiento de clustering son importantes — loguear duración de cada canal y del Louvain.
