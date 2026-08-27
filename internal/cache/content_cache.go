package cache

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog"
	"golang.org/x/sync/singleflight"
)

// ContentCache provides a Redis-backed caching layer for expensive computations
// such as embeddings generation and entity extraction.
//
// The cache uses SHA-256 hashing to derive deterministic keys from input strings,
// ensuring consistent cache hits for identical inputs. Values are stored as JSON
// in Redis with configurable TTL expiration. This pattern is useful for scenarios
// where recomputing results is costly and staleness is acceptable within the
// TTL window.
type ContentCache struct {
	client       *redis.Client
	logger       zerolog.Logger
	defaultTTL   time.Duration
	stageVersion string
	sf           singleflight.Group
}

// NewContentCache creates a new ContentCache with the given Redis client and default TTL.
//
// Parameters:
//   - client: Redis client for storing and retrieving cached values
//   - defaultTTL: Time-to-live duration for cache entries; entries are automatically
//     expired by Redis after this duration
//
// The returned cache is ready to use immediately.
func NewContentCache(client *redis.Client, defaultTTL time.Duration) *ContentCache {
	return &ContentCache{
		client:       client,
		defaultTTL:   defaultTTL,
		stageVersion: "v1",
	}
}

// SetStageVersion sets the explicit stage version used to namespace cache keys.
// Keys are formatted as artifact:content:{stageVersion}:{hash}, so bumping the
// version invalidates previously cached entries under the old namespace.
func (c *ContentCache) SetStageVersion(v string) {
	c.stageVersion = v
}

// keyPrefix returns the versioned namespace prefix for cache keys.
func (c *ContentCache) keyPrefix() string {
	if c.stageVersion == "" {
		return "artifact:content:v1"
	}
	return fmt.Sprintf("artifact:content:%s", c.stageVersion)
}

// GetOrCompute implements a cache-aside (lazy-loading) pattern with singleflight
// protection: retrieves a cached value if available, otherwise computes it by
// invoking the provided function and stores the result for future use.
//
// The cache key is derived from the input key using SHA-256 hashing. If a cached
// value exists and is successfully unmarshaled, it is returned immediately without
// invoking compute(). If no cached value exists, singleflight ensures that only
// ONE computation runs even if multiple goroutines request the same key concurrently,
// preventing the "thundering herd" problem where 100 requests could trigger 100
// expensive computations.
//
// Parameters:
//   - ctx: Context for Redis operations
//   - key: Input string to use as the cache key (hashed internally)
//   - compute: Function that produces a cacheable result; called if not cached
//
// Returns:
//   - The cached or newly computed value
//   - An error if the compute function fails, JSON marshaling fails, or Redis operations fail
//
// Note: Singleflight deduplication applies only while compute() is running.
// Once the result is cached, subsequent calls retrieve it directly from Redis.
func (c *ContentCache) GetOrCompute(ctx context.Context, key string, compute func() (interface{}, error)) (interface{}, error) {
	hash := c.computeHash(key)
	cacheKey := fmt.Sprintf("%s:%s", c.keyPrefix(), hash)

	cached, err := c.client.Get(ctx, cacheKey).Bytes()
	if err == nil && len(cached) > 0 {
		var result interface{}
		if err := json.Unmarshal(cached, &result); err == nil {
			return result, nil
		}
	}

	// Use singleflight to prevent thundering herd: only one compute() executes
	// per unique key, even with concurrent requests
	v, err, _ := c.sf.Do(hash, func() (interface{}, error) {
		result, err := compute()
		if err != nil {
			return nil, err
		}

		data, err := json.Marshal(result)
		if err != nil {
			return nil, fmt.Errorf("failed to marshal result: %w", err)
		}

		if err := c.client.Set(ctx, cacheKey, data, c.defaultTTL).Err(); err != nil {
			return nil, fmt.Errorf("failed to cache result: %w", err)
		}

		return result, nil
	})

	if err != nil {
		return nil, err
	}

	return v, nil
}

// Get retrieves a cached value without computing it.
//
// The cache key is derived from the input key using SHA-256 hashing. If no cached
// value exists or unmarshaling the cached JSON fails, nil is returned with nil error.
// Only actual Redis errors (network, protocol, etc.) are returned as non-nil errors.
//
// Parameters:
//   - ctx: Context for Redis operations
//   - key: Input string to use as the cache key (hashed internally)
//
// Returns:
//   - The cached value if found and unmarshaled successfully, otherwise nil
//   - An error if a Redis operation fails; nil if key not found or unmarshal fails
func (c *ContentCache) Get(ctx context.Context, key string) (interface{}, error) {
	hash := c.computeHash(key)
	cacheKey := fmt.Sprintf("%s:%s", c.keyPrefix(), hash)

	cached, err := c.client.Get(ctx, cacheKey).Bytes()
	if err != nil {
		if err == redis.Nil {
			return nil, nil
		}
		return nil, err
	}

	var result interface{}
	if err := json.Unmarshal(cached, &result); err != nil {
		return nil, err
	}

	return result, nil
}

// Set stores a value in the cache with the default TTL.
//
// The cache key is derived from the input key using SHA-256 hashing. The value
// is JSON-marshaled before storage in Redis. The entry will automatically expire
// after the default TTL duration configured at cache creation.
//
// Parameters:
//   - ctx: Context for Redis operations
//   - key: Input string to use as the cache key (hashed internally)
//   - value: Value to cache; must be JSON-serializable
//
// Returns:
//   - An error if JSON marshaling or the Redis Set operation fails; nil on success
func (c *ContentCache) Set(ctx context.Context, key string, value interface{}) error {
	hash := c.computeHash(key)
	cacheKey := fmt.Sprintf("%s:%s", c.keyPrefix(), hash)

	data, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("failed to marshal value: %w", err)
	}

	return c.client.Set(ctx, cacheKey, data, c.defaultTTL).Err()
}

// Invalidate removes a cached entry by key, immediately expiring it.
//
// The cache key is derived from the input key using SHA-256 hashing. If the key
// does not exist, the operation succeeds without error. Only actual Redis errors
// (network, protocol, etc.) are returned as errors.
//
// Parameters:
//   - ctx: Context for Redis operations
//   - key: Input string to use as the cache key (hashed internally)
//
// Returns:
//   - An error if the Redis Del operation fails; nil on success or if key not found
func (c *ContentCache) Invalidate(ctx context.Context, key string) error {
	hash := c.computeHash(key)
	cacheKey := fmt.Sprintf("%s:%s", c.keyPrefix(), hash)

	return c.client.Del(ctx, cacheKey).Err()
}

// computeHash derives a deterministic cache key from input by computing its SHA-256 hash.
// The hash is returned as a hexadecimal string for use in Redis key construction.
func (c *ContentCache) computeHash(content string) string {
	hash := sha256.Sum256([]byte(content))
	return hex.EncodeToString(hash[:])
}
