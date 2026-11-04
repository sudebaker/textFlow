package models

import (
	"fmt"
	"strings"
)

// Accepted processing profiles.
const (
	ProfileRag  = "rag"
	ProfileFull = "full"
)

// NormalizeProfile validates and normalizes a processing profile string.
// Accepts rag|full (case-insensitive). Empty values default to "full";
// any other value is rejected.
func NormalizeProfile(profile string) (string, error) {
	normalized := strings.ToLower(strings.TrimSpace(profile))
	switch normalized {
	case "":
		return ProfileFull, nil
	case ProfileRag, ProfileFull:
		return normalized, nil
	default:
		return "", fmt.Errorf("invalid profile %q (valid: rag, full)", profile)
	}
}
