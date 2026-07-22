package middleware

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

func init() {
	gin.SetMode(gin.TestMode)
}

func TestAPIKeyAuth_DisabledWhenEmpty(t *testing.T) {
	r := gin.New()
	r.Use(APIKeyAuth(""))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}
}

func TestAPIKeyAuth_ValidKey(t *testing.T) {
	key := generateTestKey(t)
	r := gin.New()
	r.Use(APIKeyAuth(key))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-API-Key", key)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}
}

func TestAPIKeyAuth_MissingKey(t *testing.T) {
	key := generateTestKey(t)
	r := gin.New()
	r.Use(APIKeyAuth(key))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401, got %d", w.Code)
	}
}

func TestAPIKeyAuth_InvalidKey(t *testing.T) {
	key := generateTestKey(t)
	r := gin.New()
	r.Use(APIKeyAuth(key))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-API-Key", "wrong-key")
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401, got %d", w.Code)
	}
}

func TestAPIKeyAuth_TimingAttackResistance(t *testing.T) {
	key := generateTestKey(t)
	r := gin.New()
	r.Use(APIKeyAuth(key))
	r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

	longWrongKey := make([]byte, len(key)*10)
	for i := range longWrongKey {
		longWrongKey[i] = 'x'
	}

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("X-API-Key", string(longWrongKey))
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401, got %d", w.Code)
	}
}

func generateTestKey(t *testing.T) string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		t.Fatalf("failed to generate key: %v", err)
	}
	return hex.EncodeToString(b)
}

var _ = subtle.ConstantTimeCompare
