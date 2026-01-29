package middleware

import (
	"context"
	"net/http"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"golang.org/x/time/rate"
)

type limiterEntry struct {
	limiter  *rate.Limiter
	lastSeen time.Time
}

type RateLimiter struct {
	limiters map[string]*limiterEntry
	mu       sync.RWMutex
	limit    rate.Limit
	burst    int
	ttl      time.Duration
	ctx      context.Context
	cancel   context.CancelFunc
}

func NewRateLimiter(limit rate.Limit, burst int) *RateLimiter {
	return NewRateLimiterWithTTL(limit, burst, 1*time.Hour)
}

func NewRateLimiterWithTTL(limit rate.Limit, burst int, ttl time.Duration) *RateLimiter {
	ctx, cancel := context.WithCancel(context.Background())

	rl := &RateLimiter{
		limiters: make(map[string]*limiterEntry),
		limit:    limit,
		burst:    burst,
		ttl:      ttl,
		ctx:      ctx,
		cancel:   cancel,
	}

	// Start cleanup goroutine
	go rl.cleanupLoop()

	return rl
}

func (rl *RateLimiter) getLimiter(key string) *rate.Limiter {
	now := time.Now()

	// Try read lock first
	rl.mu.RLock()
	entry, exists := rl.limiters[key]
	rl.mu.RUnlock()

	if exists {
		// Update lastSeen with write lock
		rl.mu.Lock()
		entry.lastSeen = now
		rl.mu.Unlock()
		return entry.limiter
	}

	// Create new limiter with write lock
	rl.mu.Lock()
	defer rl.mu.Unlock()

	// Double-check after acquiring write lock
	if entry, exists = rl.limiters[key]; exists {
		entry.lastSeen = now
		return entry.limiter
	}

	// Create new entry
	limiter := rate.NewLimiter(rl.limit, rl.burst)
	rl.limiters[key] = &limiterEntry{
		limiter:  limiter,
		lastSeen: now,
	}

	return limiter
}

// cleanupLoop runs periodically to remove old entries
func (rl *RateLimiter) cleanupLoop() {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()

	for {
		select {
		case <-rl.ctx.Done():
			return
		case <-ticker.C:
			rl.cleanup()
		}
	}
}

// cleanup removes entries that haven't been seen within the TTL
func (rl *RateLimiter) cleanup() {
	rl.mu.Lock()
	defer rl.mu.Unlock()

	now := time.Now()
	deleted := 0

	for key, entry := range rl.limiters {
		if now.Sub(entry.lastSeen) > rl.ttl {
			delete(rl.limiters, key)
			deleted++
		}
	}

	// Optional: log cleanup stats
	if deleted > 0 {
		// Could add logging here if logger is available
		// logger.Debug().Msgf("RateLimiter cleanup: removed %d old entries, %d remaining", deleted, len(rl.limiters))
	}
}

// Stop stops the cleanup goroutine (call when shutting down)
func (rl *RateLimiter) Stop() {
	rl.cancel()
}

// Size returns the current number of tracked limiters (for monitoring)
func (rl *RateLimiter) Size() int {
	rl.mu.RLock()
	defer rl.mu.RUnlock()
	return len(rl.limiters)
}

func (rl *RateLimiter) Middleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		key := c.ClientIP()
		limiter := rl.getLimiter(key)

		if !limiter.Allow() {
			c.AbortWithStatusJSON(http.StatusTooManyRequests, gin.H{
				"error":       "rate_limit_exceeded",
				"message":     "Too many requests, please try again later",
				"retry_after": "1s",
			})
			return
		}

		c.Next()
	}
}
