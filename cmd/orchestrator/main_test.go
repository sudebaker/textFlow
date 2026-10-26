package main

import (
	"testing"

	"textflow/internal/models"
)

func TestCalculateCurrentStep(t *testing.T) {
	order := []string{"extraction", "embeddings", "entities", "metadata", "inferences"}

	tests := []struct {
		name     string
		steps    map[string]string
		expected string
	}{
		{
			name:     "empty map returns empty string",
			steps:    map[string]string{},
			expected: "",
		},
		{
			name:     "single step with processing status returns that step",
			steps:    map[string]string{"extraction": "processing"},
			expected: "extraction",
		},
		{
			name: "all steps completed returns last one in pipeline order",
			steps: map[string]string{
				"extraction": "completed",
				"embeddings": "completed",
				"entities":   "completed",
				"metadata":   "completed",
				"inferences": "completed",
			},
			expected: "inferences",
		},
		{
			name: "step not in pipelineOrder is ignored",
			steps: map[string]string{
				"unknown_step": "processing",
				"extraction":   "completed",
			},
			expected: "extraction",
		},
		{
			name: "one step processing others completed returns processing step",
			steps: map[string]string{
				"extraction": "completed",
				"embeddings": "completed",
				"entities":   "processing",
				"metadata":   "completed",
			},
			expected: "entities",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := calculateCurrentStep(tc.steps, order)
			if got != tc.expected {
				t.Errorf("calculateCurrentStep(%v, order) = %q; want %q", tc.steps, got, tc.expected)
			}
		})
	}
}

func TestNormalizeProfile(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    string
		wantErr bool
	}{
		{name: "empty defaults to full", input: "", want: "full"},
		{name: "whitespace defaults to full", input: "   ", want: "full"},
		{name: "rag passes through", input: "rag", want: "rag"},
		{name: "full passes through", input: "full", want: "full"},
		{name: "case insensitive", input: "RAG", want: "rag"},
		{name: "trimmed", input: "  Full  ", want: "full"},
		{name: "fast removed", input: "fast", wantErr: true},
		{name: "balanced removed", input: "balanced", wantErr: true},
		{name: "unknown rejected", input: "bogus", wantErr: true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := normalizeProfile(tc.input)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("normalizeProfile(%q) error = nil; want error", tc.input)
				}
				return
			}
			if err != nil {
				t.Fatalf("normalizeProfile(%q) unexpected error: %v", tc.input, err)
			}
			if got != tc.want {
				t.Errorf("normalizeProfile(%q) = %q; want %q", tc.input, got, tc.want)
			}
		})
	}
}

func TestBuildCompressedResultsKeepsMediaFields(t *testing.T) {
	description := "A cat on a sofa"
	results := &models.JobResults{
		JobID:              "j1",
		Status:             "completed",
		CreatedAt:          "now",
		ImageOcrText:       "visible text",
		ImageDescription:   &description,
		MetadataImage:      &models.ImageMetadata{Language: "es", Description: description},
		MetadataAudio:      &models.AudioMetadata{Language: "es"},
		AudioSegments:      []models.AudioSegment{{}},
		EmbeddingsDegraded: true,
	}

	response := buildCompressedResults(results, nil)

	if response["image_ocr_text"] != "visible text" {
		t.Fatalf("image_ocr_text = %v", response["image_ocr_text"])
	}
	gotDescription, ok := response["image_description"].(*string)
	if !ok || gotDescription == nil || *gotDescription != description {
		t.Fatalf("image_description = %v", response["image_description"])
	}
	if _, ok := response["metadata_image"]; !ok {
		t.Fatal("metadata_image missing")
	}
	if _, ok := response["metadata_audio"]; !ok {
		t.Fatal("metadata_audio missing")
	}
	if _, ok := response["audio_segments"]; !ok {
		t.Fatal("audio_segments missing")
	}
	if response["embeddings_degraded"] != true {
		t.Fatalf("embeddings_degraded = %v", response["embeddings_degraded"])
	}
}

func TestBuildCompressedResultsKeepsNullDescriptionForImageRag(t *testing.T) {
	results := &models.JobResults{
		JobID:         "j1",
		Status:        "completed",
		MetadataImage: &models.ImageMetadata{Language: "es"},
	}

	response := buildCompressedResults(results, nil)

	description, ok := response["image_description"].(*string)
	if !ok {
		t.Fatal("image_description missing for image job")
	}
	if description != nil {
		t.Fatalf("image_description = %v, want null", *description)
	}
}
