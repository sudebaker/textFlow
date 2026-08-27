package cache

import (
	"testing"
	"time"
)

func TestContentCacheKeyPrefixDefault(t *testing.T) {
	c := NewContentCache(nil, time.Minute)
	got := c.keyPrefix()
	want := "artifact:content:v1"
	if got != want {
		t.Fatalf("default keyPrefix = %q, want %q", got, want)
	}
}

func TestContentCacheKeyPrefixCustomStageVersion(t *testing.T) {
	c := NewContentCache(nil, time.Minute)
	c.SetStageVersion("v2")
	got := c.keyPrefix()
	want := "artifact:content:v2"
	if got != want {
		t.Fatalf("custom keyPrefix = %q, want %q", got, want)
	}
}
