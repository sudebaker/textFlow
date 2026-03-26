# Plan de Integración SIIT → UCO GraphRAG

**Fecha:** 2026-03-26  
**Contexto:** SIIT (Proyecto-Unificado compartido en Drive) tiene un pipeline de extracción+inferencias más maduro. UCO GraphRAG tiene mejor arquitectura de consulta, agente autónomo y RAG híbrido. Son complementarios.

---

## 1. 🧠 Pipeline de Extracción en 3 Fases (alta prioridad)

**Qué tiene SIIT que UCO no:** Pipeline secuencial estructurado con GLiNER NER + Regex CEIO → relaciones LLM → inferencias micro/meso/macro con clustering episódico.

**Qué aportaría:** La extracción actual de UCO es híbrida (Regex+LLM) pero sin capa de **inferencias jerarquizadas**. Añadir micro→meso→macro daría al agente autónomo productos de inteligencia de mayor nivel para generar hipótesis, sin consultar el grafo crudo.

**Esfuerzo estimado:** Alto. Requiere adaptar el motor de inferencias de SIIT al schema de Memgraph/Qdrant.

---

## 2. 📊 Clustering Episódico (alta prioridad)

**Qué tiene SIIT:** Agrupación de micro-inferencias en episodios factuales usando 3 canales ponderados:
- Canal A — Co-ocurrencia de entidades (Jaccard, peso 0.5)
- Canal B — Proximidad en grafo 1-2 hops (Neo4j, peso 0.3)
- Canal C — Similitud semántica coseno embeddings 2560d (peso 0.2)
- Detección de comunidades: Louvain (resolution=2.5)
- Resultado benchmark real: 647 micros → 45 episodios en 3.5s

**Qué aportaría:** El `AutonomousHypothesisInvestigator` trabaja sobre entidades y relaciones sueltas. Con episodios pre-agrupados, las hipótesis tendrían contexto narrativo coherente y las contradicciones serían más fáciles de detectar.

**Esfuerzo estimado:** Medio. El stack ya tiene Memgraph + Qdrant; los tres canales encajan directamente.

---

## 3. 📁 Metadata de Origen por Fuente (media prioridad)

**Qué tiene SIIT:** Cada documento lleva su fuente (Catastro, Notariado, Registradores, Insight View...) como metadato de operación. Entidades y relaciones saben de qué organismo vienen.

**Qué aportaría:** El sistema de citaciones `[Fuente N]` mejoraría — actualmente cita el chunk, pero podría citar también el **organismo/fuente**. Para investigaciones patrimoniales es crítico: una escritura notarial no tiene el mismo peso probatorio que un informe mercantil.

**Esfuerzo estimado:** Bajo-medio. Principalmente cambio de schema e ingesta.

---

## 4. 📋 Sistema de Informes en Dos Velocidades (media prioridad)

**Qué tiene SIIT:**
- **TaskAnalyzer** → informe rápido (<10s, síncrono, filtros específicos)
- **AnalysisOrchestrator** → análisis profundo (30min-2h, asíncrono, programado nocturno)

**Qué aportaría:** El `IntelligenceService` actual genera informes sin esta separación. Para un equipo operativo: "dame las transacciones sospechosas del caso 42 ya" vs "analiza todo el caso esta noche" es una diferencia real de uso.

**Esfuerzo estimado:** Medio. Celery ya soporta workers asíncronos; añadir lógica de scheduling nocturno.

---

## 5. 🌍 Traducción Especializada Multilingüe (baja prioridad)

**Qué tiene SIIT:** Aya-Expanse-8B (23 idiomas nativos) en GPU T4 dedicada + validación sintáctica con Phi-4-mini en CPU.

**Qué aportaría:** Para documentos en árabe, ruso, chino o rumano (frecuentes en crimen organizado), traducción integrada en el pipeline de ingesta evita el paso manual previo.

**Esfuerzo estimado:** Bajo (si hay GPU disponible). Servicio independiente que se conecta en preproceso.

---

## Orden de Implementación Sugerido

| Fase | Feature | Impacto | Esfuerzo |
|------|---------|---------|---------|
| 1 | Clustering episódico | 🔴 Alto | Medio |
| 2 | Micro→meso→macro inferencias | 🔴 Alto | Alto |
| 3 | Metadata de fuente en citaciones | 🟡 Medio | Bajo |
| 4 | Informes dos velocidades | 🟡 Medio | Medio |
| 5 | Traducción multilingüe | 🟢 Complementario | Bajo |

---

## Referencias
- **UCO GraphRAG:** `git@github.com:sudebaker/ocugraphrag.git`
- **SIIT:** `gdrive:Proyecto-Unificado` (compartido, acceso vía rclone)
- **Benchmark SIIT:** `documentation/INFORME_BENCHMARK_INFERENCIAS_20260326.md` — caso real CASO0785/2022, 28 docs, ~53 min end-to-end, RTX 3090
