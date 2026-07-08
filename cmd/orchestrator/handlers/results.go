package handlers

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
	"textflow/internal/models"
)

var resultsPath string

func SetResultsPath(path string) {
	resultsPath = path
}

type GraphResponse struct {
	SchemaVersion string `json:"schema_version"`
	JobID         string `json:"job_id"`
	Nodes         []Node `json:"nodes"`
	Edges         []Edge `json:"edges"`
}

type Node struct {
	ID    string         `json:"id"`
	Label string         `json:"label"`
	Props map[string]any `json:"props,omitempty"`
}

type Edge struct {
	From string `json:"from"`
	To   string `json:"to"`
	Type string `json:"type"`
}

type VectorsResponse struct {
	SchemaVersion string            `json:"schema_version"`
	JobID         string            `json:"job_id"`
	Chunks        []ChunkVector     `json:"chunks"`
	Inferences    []InferenceVector `json:"inferences"`
}

type ChunkVector struct {
	ChunkID   string    `json:"chunk_id"`
	Text      string    `json:"text,omitempty"`
	Embedding []float32 `json:"embedding,omitempty"`
}

type InferenceVector struct {
	InferenceID string    `json:"inference_id"`
	ChunkID     string    `json:"chunk_id"`
	Text        string    `json:"text,omitempty"`
	Embedding   []float32 `json:"embedding,omitempty"`
}

type EntitiesResponse struct {
	SchemaVersion string       `json:"schema_version"`
	JobID         string       `json:"job_id"`
	Entities      []EntityFlat `json:"entities"`
}

type EntityFlat struct {
	EntityID   string  `json:"entity_id"`
	Label      string  `json:"label"`
	Text       string  `json:"text"`
	Confidence float32 `json:"confidence"`
}

type InferencesResponse struct {
	SchemaVersion string          `json:"schema_version"`
	JobID         string          `json:"job_id"`
	Inferences    []InferenceFlat `json:"inferences"`
}

type InferenceFlat struct {
	InferenceID  string   `json:"inference_id"`
	ChunkID      string   `json:"chunk_id"`
	Text         string   `json:"text"`
	Confidence   float32  `json:"confidence"`
	EntityRefs   []string `json:"entity_refs,omitempty"`
	EntityIDRefs []string `json:"entity_id_refs,omitempty"`
}

func loadJobResults(jobID string, basePath string) (*models.JobResults, error) {
	data, err := os.ReadFile(filepath.Join(basePath, jobID+".json"))
	if err != nil {
		return nil, err
	}
	var results models.JobResults
	if err := json.Unmarshal(data, &results); err != nil {
		return nil, err
	}
	results.JobID = jobID
	return &results, nil
}

func toGraphView(results *models.JobResults) *GraphResponse {
	totalInferences := 0
	for _, chunk := range results.Chunks {
		totalInferences += len(chunk.Inferences)
	}

	capacityNodes := int(float64(1+len(results.Chunks)+len(results.Entities)+totalInferences) * 1.1)
	capacityEdges := int(float64(len(results.Chunks)+len(results.Entities)+totalInferences*2) * 1.1)

	nodes := make([]Node, 0, capacityNodes)
	edges := make([]Edge, 0, capacityEdges)

	docID := "doc_" + results.JobID
	title := ""
	if results.DocumentMetadata != nil {
		if t, ok := results.DocumentMetadata["title"].(string); ok {
			title = t
		}
	}
	nodes = append(nodes, Node{
		ID:    docID,
		Label: "Document",
		Props: map[string]any{"title": title},
	})

	for _, chunk := range results.Chunks {
		nodes = append(nodes, Node{
			ID:    chunk.ChunkID,
			Label: "Chunk",
			Props: map[string]any{"start_offset": chunk.StartOffset, "text_length": len(chunk.Text)},
		})
		edges = append(edges, Edge{From: docID, To: chunk.ChunkID, Type: "HAS_CHUNK"})
	}

	for entityID, entity := range results.Entities {
		nodes = append(nodes, Node{
			ID:    entityID,
			Label: "Entity",
			Props: map[string]any{"label": entity.Label, "text": entity.Text, "confidence": entity.Confidence},
		})
		edges = append(edges, Edge{From: docID, To: entityID, Type: "HAS_ENTITY"})
	}

	for _, chunk := range results.Chunks {
		for idx, inf := range chunk.Inferences {
			infID := fmt.Sprintf("inf_%s_%d", chunk.ChunkID, idx)
			nodes = append(nodes, Node{
				ID:    infID,
				Label: "Inference",
				Props: map[string]any{"text": inf.Text, "confidence": inf.Confidence},
			})
			edges = append(edges, Edge{From: chunk.ChunkID, To: infID, Type: "HAS_INFERENCE"})
			if len(inf.EntityIDs) > 0 {
				for _, refID := range inf.EntityIDs {
					edges = append(edges, Edge{From: infID, To: refID, Type: "REFERS_TO"})
				}
			}
		}
	}

	return &GraphResponse{
		SchemaVersion: results.SchemaVersion,
		JobID:         results.JobID,
		Nodes:         nodes,
		Edges:         edges,
	}
}

// GraphHandler returns the document graph as nodes and edges for Memgraph ingestion.
// @Summary Get document graph
// @Description Returns nodes and edges representing the document structure for graph database ingestion.
// @Tags documents
// @Produce json
// @Param id path string true "Job ID"
// @Success 200 {object} GraphResponse
// @Failure 400 {object} models.ErrorResponse
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id}/graph [get]
func GraphHandler(c *gin.Context) {
	jobID := c.Param("id")
	if !isValidJobID(jobID) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "invalid_job_id", Detail: "job ID format is invalid"})
		return
	}
	results, err := loadJobResults(jobID, resultsPath)
	if err != nil {
		if os.IsNotExist(err) {
			c.JSON(http.StatusNotFound, models.ErrorResponse{Error: "not_found", Detail: "results file not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{Error: "internal_error", Detail: "failed to read results"})
		return
	}
	if results.Status != string(models.StatusCompleted) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "job_not_ready", Detail: "job not completed"})
		return
	}

	c.JSON(http.StatusOK, toGraphView(results))
}

func isValidJobID(jobID string) bool {
	if len(jobID) != 36 {
		return false
	}
	for i, ch := range jobID {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			if ch != '-' {
				return false
			}
		} else {
			if !((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') || (ch >= 'A' && ch <= 'F')) {
				return false
			}
		}
	}
	return true
}

func parseBoolQuery(c *gin.Context, key string, defaultVal bool) bool {
	if val := c.Query(key); val != "" {
		return val != "false" && val != "0"
	}
	return defaultVal
}

func parseIntQuery(c *gin.Context, key string, defaultVal int) int {
	if val := c.Query(key); val != "" {
		if i, err := strconv.Atoi(val); err == nil {
			return i
		}
	}
	return defaultVal
}

// VectorsHandler returns chunks and inferences with embeddings for Qdrant ingestion.
// @Summary Get document vectors
// @Description Returns chunks and inferences with optional embeddings for vector database ingestion.
// @Tags documents
// @Produce json
// @Param id path string true "Job ID"
// @Param embeddings query bool false "Include embeddings (default: true)"
// @Param fields query string false "Comma-separated fields to include (chunks,inferences)"
// @Param page_chunks query int false "Page number for chunks (default: 1)"
// @Param limit_chunks query int false "Items per page for chunks (default: 100)"
// @Param page_inferences query int false "Page number for inferences (default: 1)"
// @Param limit_inferences query int false "Items per page for inferences (default: 100)"
// @Success 200 {object} VectorsResponse
// @Failure 400 {object} models.ErrorResponse
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id}/vectors [get]
func VectorsHandler(c *gin.Context) {
	jobID := c.Param("id")
	if !isValidJobID(jobID) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "invalid_job_id", Detail: "job ID format is invalid"})
		return
	}
	results, err := loadJobResults(jobID, resultsPath)
	if err != nil {
		if os.IsNotExist(err) {
			c.JSON(http.StatusNotFound, models.ErrorResponse{Error: "not_found", Detail: "results file not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{Error: "internal_error", Detail: "failed to read results"})
		return
	}
	if results.Status != string(models.StatusCompleted) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "job_not_ready", Detail: "job not completed"})
		return
	}

	embeddings := parseBoolQuery(c, "embeddings", true)
	fields := c.DefaultQuery("fields", "chunks,inferences")
	pageChunks := parseIntQuery(c, "page_chunks", 1)
	limitChunks := parseIntQuery(c, "limit_chunks", 100)
	pageInferences := parseIntQuery(c, "page_inferences", 1)
	limitInferences := parseIntQuery(c, "limit_inferences", 100)

	fieldSet := strings.Split(fields, ",")
	includeChunks := false
	includeInferences := false
	for _, f := range fieldSet {
		f = strings.TrimSpace(f)
		if f == "chunks" {
			includeChunks = true
		} else if f == "inferences" {
			includeInferences = true
		}
	}
	if !includeChunks && !includeInferences {
		includeChunks = true
		includeInferences = true
	}

	resp := VectorsResponse{
		SchemaVersion: results.SchemaVersion,
		JobID:         results.JobID,
	}

	if includeChunks {
		start := (pageChunks - 1) * limitChunks
		end := start + limitChunks
		if start > len(results.Chunks) {
			resp.Chunks = []ChunkVector{}
		} else {
			if end > len(results.Chunks) {
				end = len(results.Chunks)
			}
			for _, chunk := range results.Chunks[start:end] {
				cv := ChunkVector{ChunkID: chunk.ChunkID}
				if embeddings {
					cv.Embedding = chunk.Embeddings
				}
				cv.Text = chunk.Text
				resp.Chunks = append(resp.Chunks, cv)
			}
		}
	}

	if includeInferences {
		allInferences := []InferenceVector{}
		for _, chunk := range results.Chunks {
			for idx, inf := range chunk.Inferences {
				infID := fmt.Sprintf("inf_%s_%d", chunk.ChunkID, idx)
				iv := InferenceVector{InferenceID: infID, ChunkID: chunk.ChunkID}
				if embeddings {
					iv.Embedding = inf.Embedding
				}
				iv.Text = inf.Text
				allInferences = append(allInferences, iv)
			}
		}
		start := (pageInferences - 1) * limitInferences
		end := start + limitInferences
		if start > len(allInferences) {
			resp.Inferences = []InferenceVector{}
		} else {
			if end > len(allInferences) {
				end = len(allInferences)
			}
			resp.Inferences = allInferences[start:end]
		}
	}

	c.JSON(http.StatusOK, resp)
}

// EntitiesHandler returns a flat list of entities for the document.
// @Summary Get document entities
// @Description Returns a flat list of all entities extracted from the document.
// @Tags documents
// @Produce json
// @Param id path string true "Job ID"
// @Success 200 {object} EntitiesResponse
// @Failure 400 {object} models.ErrorResponse
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id}/entities [get]
func EntitiesHandler(c *gin.Context) {
	jobID := c.Param("id")
	if !isValidJobID(jobID) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "invalid_job_id", Detail: "job ID format is invalid"})
		return
	}
	results, err := loadJobResults(jobID, resultsPath)
	if err != nil {
		if os.IsNotExist(err) {
			c.JSON(http.StatusNotFound, models.ErrorResponse{Error: "not_found", Detail: "results file not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{Error: "internal_error", Detail: "failed to read results"})
		return
	}
	if results.Status != string(models.StatusCompleted) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "job_not_ready", Detail: "job not completed"})
		return
	}

	entities := []EntityFlat{}
	for entityID, entity := range results.Entities {
		entities = append(entities, EntityFlat{
			EntityID:   entityID,
			Label:      entity.Label,
			Text:       entity.Text,
			Confidence: entity.Confidence,
		})
	}

	c.JSON(http.StatusOK, EntitiesResponse{
		SchemaVersion: results.SchemaVersion,
		JobID:         results.JobID,
		Entities:      entities,
	})
}

// InferencesHandler returns a flat list of inferences with resolved entity references.
// @Summary Get document inferences
// @Description Returns a flat list of all inferences with resolved entity_id_refs for knowledge graph construction.
// @Tags documents
// @Produce json
// @Param id path string true "Job ID"
// @Param page_inferences query int false "Page number (default: 1)"
// @Param limit_inferences query int false "Items per page (default: 100)"
// @Success 200 {object} InferencesResponse
// @Failure 400 {object} models.ErrorResponse
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id}/inferences [get]
func InferencesHandler(c *gin.Context) {
	jobID := c.Param("id")
	if !isValidJobID(jobID) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "invalid_job_id", Detail: "job ID format is invalid"})
		return
	}
	results, err := loadJobResults(jobID, resultsPath)
	if err != nil {
		if os.IsNotExist(err) {
			c.JSON(http.StatusNotFound, models.ErrorResponse{Error: "not_found", Detail: "results file not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{Error: "internal_error", Detail: "failed to read results"})
		return
	}
	if results.Status != string(models.StatusCompleted) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{Error: "job_not_ready", Detail: "job not completed"})
		return
	}

	pageInferences := parseIntQuery(c, "page_inferences", 1)
	limitInferences := parseIntQuery(c, "limit_inferences", 100)

	allInferences := []InferenceFlat{}
	for _, chunk := range results.Chunks {
		for idx, inf := range chunk.Inferences {
			infID := fmt.Sprintf("inf_%s_%d", chunk.ChunkID, idx)
			flat := InferenceFlat{
				InferenceID:  infID,
				ChunkID:      chunk.ChunkID,
				Text:         inf.Text,
				Confidence:   inf.Confidence,
				EntityRefs:   inf.EntityRefs,
				EntityIDRefs: inf.EntityIDs,
			}
			allInferences = append(allInferences, flat)
		}
	}

	start := (pageInferences - 1) * limitInferences
	end := start + limitInferences
	if start > len(allInferences) {
		c.JSON(http.StatusOK, InferencesResponse{
			SchemaVersion: results.SchemaVersion,
			JobID:         results.JobID,
			Inferences:    []InferenceFlat{},
		})
		return
	}
	if end > len(allInferences) {
		end = len(allInferences)
	}

	c.JSON(http.StatusOK, InferencesResponse{
		SchemaVersion: results.SchemaVersion,
		JobID:         results.JobID,
		Inferences:    allInferences[start:end],
	})
}
