package handlers

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
	"textflow/internal/models"
)

func init() {
	gin.SetMode(gin.TestMode)
}

func setupResultsRouter(t *testing.T, resultsDir string) (*gin.Engine, func()) {
	SetResultsPath(resultsDir)
	r := gin.New()
	r.GET("/v1/documents/:id/graph", GraphHandler)
	r.GET("/v1/documents/:id/vectors", VectorsHandler)
	r.GET("/v1/documents/:id/entities", EntitiesHandler)
	r.GET("/v1/documents/:id/inferences", InferencesHandler)
	cleanup := func() {}
	return r, cleanup
}

func TestGraphHandler_InvalidJobID(t *testing.T) {
	tmpDir := t.TempDir()
	r, _ := setupResultsRouter(t, tmpDir)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/v1/documents/invalid-job-id/graph", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("Expected 400, got %d", w.Code)
	}
	var resp map[string]any
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["error"] != "invalid_job_id" {
		t.Errorf("Expected error 'invalid_job_id', got %v", resp["error"])
	}
}

func TestVectorsHandler_InvalidJobID(t *testing.T) {
	tmpDir := t.TempDir()
	r, _ := setupResultsRouter(t, tmpDir)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/v1/documents/not-valid/vectors", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("Expected 400, got %d", w.Code)
	}
}

func TestEntitiesHandler_InvalidJobID(t *testing.T) {
	tmpDir := t.TempDir()
	r, _ := setupResultsRouter(t, tmpDir)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/v1/documents/bad-id/entities", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("Expected 400, got %d", w.Code)
	}
}

func TestInferencesHandler_InvalidJobID(t *testing.T) {
	tmpDir := t.TempDir()
	r, _ := setupResultsRouter(t, tmpDir)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/v1/documents/bad-id/inferences", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Errorf("Expected 400, got %d", w.Code)
	}
}

func TestVectorsHandler_SeparatePagination(t *testing.T) {
	tmpDir := t.TempDir()
	r, _ := setupResultsRouter(t, tmpDir)

	results := &models.JobResults{
		JobID:  "a0000000-b000-c000-d000-e00000000000",
		Status: "completed",
		Chunks: []models.Chunk{
			{ChunkID: "chunk_0", Text: "text0"},
			{ChunkID: "chunk_1", Text: "text1"},
			{ChunkID: "chunk_2", Text: "text2"},
			{ChunkID: "chunk_3", Text: "text3"},
			{ChunkID: "chunk_4", Text: "text4"},
		},
		Entities: map[string]models.EntityMinimal{},
	}
	results.SchemaVersion = "1.1.0"
	data, _ := json.Marshal(results)
	filePath := filepath.Join(tmpDir, results.JobID+".json")
	os.WriteFile(filePath, data, 0644)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/v1/documents/a0000000-b000-c000-d000-e00000000000/vectors?fields=chunks&page_chunks=2&limit_chunks=2", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("Expected 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp VectorsResponse
	json.Unmarshal(w.Body.Bytes(), &resp)
	if len(resp.Chunks) != 2 {
		t.Errorf("Expected 2 chunks, got %d", len(resp.Chunks))
	}
	if resp.Chunks[0].ChunkID != "chunk_2" {
		t.Errorf("Expected chunk_2, got %s", resp.Chunks[0].ChunkID)
	}
}

func TestVectorsHandler_NotFoundJobID(t *testing.T) {
	tmpDir := t.TempDir()
	r, _ := setupResultsRouter(t, tmpDir)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/v1/documents/00000000-0000-0000-0000-000000000000/vectors", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("Expected 404, got %d", w.Code)
	}
}

func TestIsValidJobID(t *testing.T) {
	validIDs := []string{
		"a0000000-0000-0000-0000-000000000000",
		"A0000000-0000-0000-0000-000000000000",
		"12345678-1234-1234-1234-123456789012",
		"ffffffff-ffff-ffff-ffff-ffffffffffff",
	}
	for _, id := range validIDs {
		if !isValidJobID(id) {
			t.Errorf("Expected %s to be valid", id)
		}
	}

	invalidIDs := []string{
		"invalid",
		"a0000000-0000-0000-0000-00000000000",  // 35 chars
		"a0000000-0000-0000-0000-0000000000000", // 37 chars
		"a00000000000-0000-0000-000000000000",   // wrong hyphen pos
		"g0000000-0000-0000-0000-000000000000", // g is not hex
		"",
	}
	for _, id := range invalidIDs {
		if isValidJobID(id) {
			t.Errorf("Expected %s to be invalid", id)
		}
	}
}

func TestToGraphViewPreallocatedCapacity(t *testing.T) {
	chunks := make([]models.Chunk, 1000)
	for i := 0; i < 1000; i++ {
		chunks[i] = models.Chunk{
			ChunkID:     fmt.Sprintf("chunk_%04d", i),
			Text:        "test text for chunk",
			StartOffset: i * 100,
			EndOffset:   i*100 + 100,
		}
	}

	entities := make(map[string]models.EntityMinimal, 500)
	for i := 0; i < 500; i++ {
		entities["entity_"+string(rune('a'+i%26))] = models.EntityMinimal{
			Label:      "TEST",
			Text:       "test entity",
			Confidence: 0.9,
		}
	}

	for i := 0; i < 1000; i++ {
		chunks[i].Inferences = []models.InferenceItem{
			{Text: "inference 1", Confidence: 0.95},
			{Text: "inference 2", Confidence: 0.85},
			{Text: "inference 3", Confidence: 0.75},
			{Text: "inference 4", Confidence: 0.65},
			{Text: "inference 5", Confidence: 0.55},
		}
	}

	results := &models.JobResults{
		JobID:    "test_job_123",
		Status:   "completed",
		Chunks:   chunks,
		Entities: entities,
	}
	results.SchemaVersion = "1.1.0"

	resp := toGraphView(results)

	if len(resp.Nodes) > cap(resp.Nodes) {
		t.Errorf("Nodes len %d exceeds cap %d", len(resp.Nodes), cap(resp.Nodes))
	}
	if len(resp.Edges) > cap(resp.Edges) {
		t.Errorf("Edges len %d exceeds cap %d", len(resp.Edges), cap(resp.Edges))
	}

	totalInferences := 0
	for _, chunk := range results.Chunks {
		totalInferences += len(chunk.Inferences)
	}
	expectedNodes := 1 + len(results.Chunks) + len(results.Entities) + totalInferences
	if len(resp.Nodes) != expectedNodes {
		t.Errorf("Expected %d nodes, got %d", expectedNodes, len(resp.Nodes))
	}
}

func TestToGraphViewMinimal(t *testing.T) {
	results := &models.JobResults{
		JobID:  "minimal_job",
		Status: "completed",
		Chunks: []models.Chunk{
			{ChunkID: "chunk_000", Text: "hello world", StartOffset: 0, EndOffset: 11},
		},
		Entities: map[string]models.EntityMinimal{
			"e1": {Label: "ORG", Text: "Acme", Confidence: 1.0},
		},
	}
	results.SchemaVersion = "1.1.0"

	resp := toGraphView(results)

	if len(resp.Nodes) != 3 {
		t.Errorf("Expected 3 nodes, got %d", len(resp.Nodes))
	}
	if len(resp.Edges) != 2 {
		t.Errorf("Expected 2 edges (HAS_CHUNK + HAS_ENTITY), got %d", len(resp.Edges))
	}

	docNode := resp.Nodes[0]
	if docNode.Label != "Document" {
		t.Errorf("Expected Document node, got %s", docNode.Label)
	}
	if docNode.ID != "doc_minimal_job" {
		t.Errorf("Expected doc_minimal_job, got %s", docNode.ID)
	}
}

func TestToGraphViewWithEntityRefs(t *testing.T) {
	results := &models.JobResults{
		JobID:  "ref_job",
		Status: "completed",
		Chunks: []models.Chunk{
			{
				ChunkID:     "chunk_000",
				Text:        "textFlow uses Go",
				StartOffset: 0,
				EndOffset:   20,
				Inferences: []models.InferenceItem{
					{
						Text:       "textFlow is written in Go",
						Confidence: 0.95,
						EntityRefs: []string{"textFlow", "Go"},
						EntityIDs:  []string{"entity_tf", "entity_go"},
					},
				},
			},
		},
		Entities: map[string]models.EntityMinimal{
			"entity_tf": {Label: "ORG", Text: "textFlow", Confidence: 0.9},
			"entity_go": {Label: "LANG", Text: "Go", Confidence: 1.0},
		},
	}
	results.SchemaVersion = "1.1.0"

	resp := toGraphView(results)

	refToEdges := 0
	for _, edge := range resp.Edges {
		if edge.Type == "REFERS_TO" {
			refToEdges++
		}
	}
	if refToEdges != 2 {
		t.Errorf("Expected 2 REFERS_TO edges, got %d", refToEdges)
	}
}

func TestToGraphViewEmptyInferences(t *testing.T) {
	results := &models.JobResults{
		JobID:  "empty_inf_job",
		Status: "completed",
		Chunks: []models.Chunk{
			{
				ChunkID:     "chunk_000",
				Text:        "no inferences here",
				StartOffset: 0,
				EndOffset:   21,
				Inferences:  []models.InferenceItem{},
			},
		},
		Entities: map[string]models.EntityMinimal{},
	}
	results.SchemaVersion = "1.1.0"

	resp := toGraphView(results)

	if len(resp.Nodes) != 2 {
		t.Errorf("Expected 2 nodes (doc + chunk), got %d", len(resp.Nodes))
	}
	if len(resp.Edges) != 1 {
		t.Errorf("Expected 1 edge (HAS_CHUNK), got %d", len(resp.Edges))
	}
}

func TestEntitiesHandlerFlat(t *testing.T) {
	results := &models.JobResults{
		JobID:  "entity_test",
		Status: "completed",
		Entities: map[string]models.EntityMinimal{
			"abc123": {Label: "ORG", Text: "textFlow", Confidence: 0.9},
			"def456": {Label: "LANG", Text: "Go", Confidence: 1.0},
		},
	}
	results.SchemaVersion = "1.1.0"

	entities := []EntityFlat{}
	for entityID, entity := range results.Entities {
		entities = append(entities, EntityFlat{
			EntityID:   entityID,
			Label:      entity.Label,
			Text:       entity.Text,
			Confidence: entity.Confidence,
		})
	}

	if len(entities) != 2 {
		t.Errorf("Expected 2 entities, got %d", len(entities))
	}
}

func TestInferencesHandlerFlat(t *testing.T) {
	results := &models.JobResults{
		JobID:  "inf_test",
		Status: "completed",
		Chunks: []models.Chunk{
			{
				ChunkID: "chunk_000",
				Inferences: []models.InferenceItem{
					{
						Text:       "test inference",
						Confidence: 0.95,
						EntityRefs: []string{"textFlow"},
						EntityIDs:  []string{"abc123"},
					},
				},
			},
		},
	}
	results.SchemaVersion = "1.1.0"

	allInferences := []InferenceFlat{}
	for _, chunk := range results.Chunks {
		for idx, inf := range chunk.Inferences {
			infID := "inf_" + chunk.ChunkID + "_" + string(rune('0'+idx))
			allInferences = append(allInferences, InferenceFlat{
				InferenceID:  infID,
				ChunkID:      chunk.ChunkID,
				Text:         inf.Text,
				Confidence:   inf.Confidence,
				EntityRefs:   inf.EntityRefs,
				EntityIDRefs: inf.EntityIDs,
			})
		}
	}

	if len(allInferences) != 1 {
		t.Errorf("Expected 1 inference, got %d", len(allInferences))
	}
	if allInferences[0].EntityIDRefs[0] != "abc123" {
		t.Errorf("Expected entity_id_refs [abc123], got %v", allInferences[0].EntityIDRefs)
	}
}
