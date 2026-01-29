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
)

type ContentCache struct {
	client     *redis.Client
	logger     zerolog.Logger
	defaultTTL time.Duration
}

func NewContentCache(client *redis.Client, defaultTTL time.Duration) *ContentCache {
	return &ContentCache{
		client:     client,
		defaultTTL: defaultTTL,
	}
}

func (c *ContentCache) GetOrCompute(ctx context.Context, key string, compute func() (interface{}, error)) (interface{}, error) {
	hash := c.computeHash(key)
	cacheKey := fmt.Sprintf("content:%s", hash)

	cached, err := c.client.Get(ctx, cacheKey).Bytes()
	if err == nil && len(cached) > 0 {
		var result interface{}
		if err := json.Unmarshal(cached, &result); err == nil {
			return result, nil
		}
	}

	result, err := compute()
	if err != nil {
		return nil, err
	}

	data, err := json.Marshal(result)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal result: %w", err)
	}

	c.client.Set(ctx, cacheKey, data, c.defaultTTL)

	return result, nil
}

func (c *ContentCache) Get(ctx context.Context, key string) (interface{}, error) {
	hash := c.computeHash(key)
	cacheKey := fmt.Sprintf("content:%s", hash)

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

func (c *ContentCache) Set(ctx context.Context, key string, value interface{}) error {
	hash := c.computeHash(key)
	cacheKey := fmt.Sprintf("content:%s", hash)

	data, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("failed to marshal value: %w", err)
	}

	return c.client.Set(ctx, cacheKey, data, c.defaultTTL).Err()
}

func (c *ContentCache) Invalidate(ctx context.Context, key string) error {
	hash := c.computeHash(key)
	cacheKey := fmt.Sprintf("content:%s", hash)

	return c.client.Del(ctx, cacheKey).Err()
}

func (c *ContentCache) computeHash(content string) string {
	hash := sha256.Sum256([]byte(content))
	return hex.EncodeToString(hash[:])
}
