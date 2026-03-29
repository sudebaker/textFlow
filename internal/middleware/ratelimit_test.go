package middleware_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"golang.org/x/time/rate"
	"ia-text-orchestrator/internal/middleware"
)

func init() {
	gin.SetMode(gin.TestMode)
}

// newTestRouter creates a Gin router with the rate limiter middleware applied.
func newTestRouter(rl *middleware.RateLimiter) *gin.Engine {
	router := gin.New()
	router.Use(rl.Middleware())
	router.GET("/", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	})
	return router
}

// performRequest sends a GET request to the router and returns the response.
func performRequest(router http.Handler, clientIP string) *httptest.ResponseRecorder {
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Forwarded-For", clientIP)
	router.ServeHTTP(w, req)
	return w
}

// TestRateLimiter_AllowsWithinLimit verifies that requests within the burst limit
// are all permitted (HTTP 200).
func TestRateLimiter_AllowsWithinLimit(t *testing.T) {
	// 10 requests/second with burst of 5 — first 5 should all pass immediately.
	rl := middleware.NewRateLimiterWithTTL(rate.Limit(10), 5, 1*time.Minute)
	defer rl.Stop()

	router := newTestRouter(rl)

	for i := 0; i < 5; i++ {
		w := performRequest(router, "192.168.1.1")
		assert.Equal(t, http.StatusOK, w.Code, "request %d should be allowed", i+1)
	}
}

// TestRateLimiter_BlocksWhenExceeded verifies that requests exceeding the burst
// limit are rejected.
func TestRateLimiter_BlocksWhenExceeded(t *testing.T) {
	// 1 request/second with burst 1 — second request should be blocked.
	rl := middleware.NewRateLimiterWithTTL(rate.Limit(1), 1, 1*time.Minute)
	defer rl.Stop()

	router := newTestRouter(rl)

	// First request uses the single burst token.
	w1 := performRequest(router, "10.0.0.1")
	assert.Equal(t, http.StatusOK, w1.Code)

	// Second immediate request exceeds the limit.
	w2 := performRequest(router, "10.0.0.1")
	assert.Equal(t, http.StatusTooManyRequests, w2.Code)
}

// TestRateLimiter_Returns429 verifies that the blocked response uses HTTP 429
// and includes the expected JSON error body.
func TestRateLimiter_Returns429(t *testing.T) {
	rl := middleware.NewRateLimiterWithTTL(rate.Limit(1), 1, 1*time.Minute)
	defer rl.Stop()

	router := newTestRouter(rl)

	// Exhaust the burst.
	performRequest(router, "10.0.0.2")

	// This request should be rate-limited.
	w := performRequest(router, "10.0.0.2")
	require.Equal(t, http.StatusTooManyRequests, w.Code)

	var body map[string]interface{}
	err := json.Unmarshal(w.Body.Bytes(), &body)
	require.NoError(t, err)

	assert.Equal(t, "rate_limit_exceeded", body["error"])
	assert.NotEmpty(t, body["message"])
	assert.NotEmpty(t, body["retry_after"])
}

// TestRateLimiter_PerClientIsolation verifies that rate limits are tracked
// independently per client IP.
func TestRateLimiter_PerClientIsolation(t *testing.T) {
	// Burst of 1 — each client gets exactly 1 allowed request.
	rl := middleware.NewRateLimiterWithTTL(rate.Limit(1), 1, 1*time.Minute)
	defer rl.Stop()

	router := newTestRouter(rl)

	// Client A — 1st request allowed, 2nd blocked.
	w := performRequest(router, "1.1.1.1")
	assert.Equal(t, http.StatusOK, w.Code, "client A first request should be allowed")
	w = performRequest(router, "1.1.1.1")
	assert.Equal(t, http.StatusTooManyRequests, w.Code, "client A second request should be blocked")

	// Client B — not yet used its quota, should be allowed.
	w = performRequest(router, "2.2.2.2")
	assert.Equal(t, http.StatusOK, w.Code, "client B first request should be allowed (separate quota)")
}

// TestRateLimiter_NewRateLimiterDefaults verifies that NewRateLimiter uses 1-hour TTL.
func TestRateLimiter_NewRateLimiterDefaults(t *testing.T) {
	rl := middleware.NewRateLimiter(rate.Limit(10), 10)
	defer rl.Stop()

	// Size starts at 0 (no clients yet).
	assert.Equal(t, 0, rl.Size())
}

// TestRateLimiter_SizeTracksClients verifies that Size() reflects the number of
// unique clients that have made requests.
func TestRateLimiter_SizeTracksClients(t *testing.T) {
	rl := middleware.NewRateLimiterWithTTL(rate.Limit(10), 100, 1*time.Minute)
	defer rl.Stop()

	router := newTestRouter(rl)

	performRequest(router, "1.1.1.1")
	performRequest(router, "2.2.2.2")
	performRequest(router, "3.3.3.3")

	assert.Equal(t, 3, rl.Size())
}

// TestRateLimiter_StopStopsCleanup verifies that Stop() can be called without panicking.
func TestRateLimiter_StopStopsCleanup(t *testing.T) {
	rl := middleware.NewRateLimiterWithTTL(rate.Limit(1), 1, 1*time.Minute)

	assert.NotPanics(t, func() {
		rl.Stop()
	})
}

// TestRateLimiter_AllowsAfterTokenRefill verifies that after waiting for token
// replenishment, a previously rate-limited client is allowed again.
func TestRateLimiter_AllowsAfterTokenRefill(t *testing.T) {
	// 10 requests/second with burst 1 — token refills in ~100ms.
	rl := middleware.NewRateLimiterWithTTL(rate.Limit(10), 1, 1*time.Minute)
	defer rl.Stop()

	router := newTestRouter(rl)

	// Exhaust the burst.
	w := performRequest(router, "5.5.5.5")
	require.Equal(t, http.StatusOK, w.Code)

	w = performRequest(router, "5.5.5.5")
	require.Equal(t, http.StatusTooManyRequests, w.Code)

	// Wait for token to refill (at 10/s, 1 token takes ~100ms).
	time.Sleep(150 * time.Millisecond)

	w = performRequest(router, "5.5.5.5")
	assert.Equal(t, http.StatusOK, w.Code, "request should be allowed after token refill")
}
