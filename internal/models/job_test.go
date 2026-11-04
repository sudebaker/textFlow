package models

import (
	"encoding/json"
	"testing"
)

func TestJobMessagePipelineVersionRoundTrip(t *testing.T) {
	in := JobMessage{
		JobID:           "job-1",
		PipelineVersion: "v1",
	}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out JobMessage
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.PipelineVersion != "v1" {
		t.Fatalf("expected pipeline_version=v1, got %q", out.PipelineVersion)
	}
}

func TestJobMessagePipelineVersionOmittedWhenEmpty(t *testing.T) {
	in := JobMessage{JobID: "job-2"}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if string(data) != `{"job_id":"job-2"}` {
		t.Fatalf("expected omitempty job_id only, got %s", data)
	}
}

func TestJobResultsMediaFieldsSerialize(t *testing.T) {
	desc := "a cat sitting on a mat"
	in := JobResults{
		JobID:            "job-1",
		Status:           "completed",
		ImageOcrText:     "TOTAL 42,00 EUR",
		ImageDescription: &desc,
		MetadataImage:    &ImageMetadata{Language: "es", Description: desc, Confidence: 0.9},
		MetadataAudio: &AudioMetadata{
			Language:        "en",
			DurationSeconds: 12.5,
			HasDiarization:  true,
			SegmentCount:    2,
		},
		AudioSegments: []AudioSegment{
			{Start: 0, End: 5, Text: "hello", Speaker: "SPEAKER_00"},
			{Start: 5, End: 12.5, Text: "world"},
		},
	}

	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, key := range []string{
		"image_ocr_text", "image_description", "metadata_image", "metadata_audio", "audio_segments",
	} {
		if _, ok := out[key]; !ok {
			t.Errorf("missing key %q in %s", key, data)
		}
	}
	if out["image_description"] != desc {
		t.Errorf("expected image_description %q, got %v", desc, out["image_description"])
	}
	if _, ok := out["schema_version"]; ok {
		t.Errorf("schema_version must be removed from JobResults: %s", data)
	}
}

func TestJobResultsImageDescriptionNullWhenNotRequested(t *testing.T) {
	in := JobResults{JobID: "job-2", Status: "completed", ImageOcrText: "text only"}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	value, ok := out["image_description"]
	if !ok {
		t.Fatalf("image_description must be present as null (not omitted): %s", data)
	}
	if value != nil {
		t.Errorf("expected null image_description, got %v", value)
	}
}

func TestJobResultsOmitsAbsentMediaFields(t *testing.T) {
	in := JobResults{JobID: "job-3", Status: "completed", Text: "document text"}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, key := range []string{
		"image_ocr_text", "metadata_image", "metadata_audio", "audio_segments",
	} {
		if _, ok := out[key]; ok {
			t.Errorf("expected %q omitted for a document result: %s", key, data)
		}
	}
}

func TestJobResultsMediaRoundTrip(t *testing.T) {
	desc := "una factura escaneada"
	in := JobResults{
		JobID:            "job-4",
		Status:           "completed",
		ImageOcrText:     "FACTURA 2026-001",
		ImageDescription: &desc,
		AudioSegments:    []AudioSegment{{Start: 0, End: 1.5, Text: "hola"}},
	}
	data, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out JobResults
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.ImageOcrText != in.ImageOcrText {
		t.Errorf("image_ocr_text round trip: got %q", out.ImageOcrText)
	}
	if out.ImageDescription == nil || *out.ImageDescription != desc {
		t.Errorf("image_description round trip: got %v", out.ImageDescription)
	}
	if len(out.AudioSegments) != 1 || out.AudioSegments[0].Text != "hola" {
		t.Errorf("audio_segments round trip: got %+v", out.AudioSegments)
	}
}
