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
