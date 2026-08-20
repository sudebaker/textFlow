package redis

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
)

// artifactRefPrefix is the prefix used to mark a Redis value as an artifact
// store reference. Values that start with it must be resolved from the
// filesystem artifact store (see resolveArtifactBytes).
const artifactRefPrefix = "sha256:"

// artifactRefRe matches a full artifact reference: the "sha256:" prefix
// followed by exactly 64 lowercase hex characters (the SHA-256 digest).
var artifactRefRe = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// isArtifactRef reports whether value is a valid artifact reference
// (sha256:<64 hex chars>). A value that merely starts with the prefix but
// does not match the full format is treated as a legacy raw payload.
func isArtifactRef(value string) bool {
	return artifactRefRe.MatchString(value)
}

// artifactRoot returns the artifact store root directory.
// It reads the ARTIFACT_PATH env var on every call so tests can override it
// with t.Setenv without requiring a rebuild or package-level state reset.
// Defaults to "/app/data/artifacts" when ARTIFACT_PATH is unset or empty.
func artifactRoot() string {
	if path := os.Getenv("ARTIFACT_PATH"); path != "" {
		return path
	}
	return "/app/data/artifacts"
}

// resolveArtifactBytes resolves a Redis value that may be an artifact
// reference into its stored bytes.
//
// If value is not an artifact reference, it returns (nil, false, nil) so the
// caller keeps using the original value (legacy raw payload, matching the
// Python resolve() semantics). If value is a reference, it reads the blob from
// the filesystem layout {root}/{digest[:2]}/{digest[2:4]}/{digest}.bin and
// returns (data, true, nil). A reference to a missing artifact yields the
// error "artifact not found: <digest>"; other filesystem failures are wrapped
// as "failed to read artifact: <err>".
func resolveArtifactBytes(ctx context.Context, value string) ([]byte, bool, error) {
	if !isArtifactRef(value) {
		return nil, false, nil
	}

	digest := value[len(artifactRefPrefix):]
	path := filepath.Join(artifactRoot(), digest[:2], digest[2:4], digest+".bin")

	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, true, fmt.Errorf("artifact not found: %s", digest)
		}
		return nil, true, fmt.Errorf("failed to read artifact: %w", err)
	}
	return data, true, nil
}
