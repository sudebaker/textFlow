package handlers

import "testing"

func TestPlanBatchProfiles_DefaultsToFull(t *testing.T) {
	req := &BatchRequest{Documents: []BatchDocument{{Filename: "a.txt"}, {Filename: "b.txt"}}}

	plan, err := planBatchProfiles(req)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(plan.Rejected) != 0 {
		t.Fatalf("unexpected rejections: %+v", plan.Rejected)
	}
	for i, p := range plan.Profiles {
		if p != "full" {
			t.Errorf("doc %d profile = %q; want full", i, p)
		}
	}
}

func TestPlanBatchProfiles_ExplicitBatchProfileInherited(t *testing.T) {
	req := &BatchRequest{
		Profile:   "rag",
		Documents: []BatchDocument{{Filename: "a.txt"}, {Filename: "b.txt"}},
	}

	plan, err := planBatchProfiles(req)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for i, p := range plan.Profiles {
		if p != "rag" {
			t.Errorf("doc %d profile = %q; want rag (inherited)", i, p)
		}
	}
}

func TestPlanBatchProfiles_PerDocumentOverride(t *testing.T) {
	req := &BatchRequest{
		Profile: "full",
		Documents: []BatchDocument{
			{Filename: "a.txt", Profile: "rag"},
			{Filename: "b.txt"},
			{Filename: "c.txt", Profile: "RAG"},
		},
	}

	plan, err := planBatchProfiles(req)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []string{"rag", "full", "rag"}
	for i, p := range plan.Profiles {
		if p != want[i] {
			t.Errorf("doc %d profile = %q; want %q", i, p, want[i])
		}
	}
	if len(plan.Rejected) != 0 {
		t.Errorf("unexpected rejections: %+v", plan.Rejected)
	}
}

func TestPlanBatchProfiles_InvalidDocumentRejectedSiblingsUnaffected(t *testing.T) {
	req := &BatchRequest{
		Profile: "full",
		Documents: []BatchDocument{
			{Filename: "good.txt"},
			{Filename: "bad.txt", Profile: "fast"},
			{Filename: "good2.txt", Profile: "rag"},
		},
	}

	plan, err := planBatchProfiles(req)
	if err != nil {
		t.Fatalf("per-document rejection must not fail the whole request: %v", err)
	}
	if len(plan.Rejected) != 1 {
		t.Fatalf("rejected = %+v; want exactly 1", plan.Rejected)
	}
	r := plan.Rejected[0]
	if r.Index != 1 || r.Filename != "bad.txt" || r.Reason == "" {
		t.Errorf("unexpected rejection: %+v", r)
	}
	if plan.Profiles[0] != "full" || plan.Profiles[2] != "rag" {
		t.Errorf("siblings changed: %v", plan.Profiles)
	}
	if plan.Profiles[1] != "" {
		t.Errorf("rejected doc profile = %q; want empty", plan.Profiles[1])
	}
}

func TestPlanBatchProfiles_RemovedAndUnknownProfilesRejected(t *testing.T) {
	req := &BatchRequest{Documents: []BatchDocument{
		{Profile: "balanced"},
		{Profile: "fast"},
		{Profile: "bogus"},
	}}

	plan, err := planBatchProfiles(req)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(plan.Rejected) != len(req.Documents) {
		t.Errorf("rejected = %d; want %d (all invalid)", len(plan.Rejected), len(req.Documents))
	}
	for _, r := range plan.Rejected {
		if r.Reason == "" {
			t.Errorf("rejection without reason: %+v", r)
		}
	}
}

func TestPlanBatchProfiles_InvalidBatchProfileFailsRequest(t *testing.T) {
	req := &BatchRequest{
		Profile:   "balanced",
		Documents: []BatchDocument{{Filename: "a.txt"}},
	}

	if _, err := planBatchProfiles(req); err == nil {
		t.Fatal("invalid batch profile must fail the request")
	}
}
