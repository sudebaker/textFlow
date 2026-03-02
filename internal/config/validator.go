package config

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"time"
)

type Validator struct {
	cfg *Config
}

type ValidationResult struct {
	Component string
	Status    string
	Message   string
	Duration  time.Duration
}

func NewValidator(cfg *Config) *Validator {
	return &Validator{cfg: cfg}
}

// ValidateAll runs all validation checks and returns results
func (v *Validator) ValidateAll() []ValidationResult {
	results := []ValidationResult{}

	// Validate Redis connection
	results = append(results, v.validateRedis())

	// Validate RabbitMQ connection
	results = append(results, v.validateRabbitMQ())

	// Validate Docling API
	results = append(results, v.validateDocling())

	// Validate Resource Manager
	results = append(results, v.validateResourceManager())

	return results
}

// IsValid returns true if all validations passed
func (v *Validator) IsValid() bool {
	results := v.ValidateAll()
	for _, r := range results {
		if r.Status != "ok" {
			return false
		}
	}
	return true
}

// validateRedis checks Redis connectivity
func (v *Validator) validateRedis() ValidationResult {
	start := time.Now()

	result := ValidationResult{
		Component: "redis",
		Status:    "pending",
	}

	conn, err := net.DialTimeout("tcp", extractHostPort(v.cfg.RedisURL), 5*time.Second)
	if err != nil {
		result.Status = "error"
		result.Message = fmt.Sprintf("Failed to connect: %v", err)
	} else {
		conn.Close()
		result.Status = "ok"
		result.Message = "Connection successful"
	}

	result.Duration = time.Since(start)
	return result
}

// validateRabbitMQ checks RabbitMQ connectivity
func (v *Validator) validateRabbitMQ() ValidationResult {
	start := time.Now()

	result := ValidationResult{
		Component: "rabbitmq",
		Status:    "pending",
	}

	conn, err := net.DialTimeout("tcp", extractHostPort(v.cfg.RabbitMQURL), 5*time.Second)
	if err != nil {
		result.Status = "error"
		result.Message = fmt.Sprintf("Failed to connect: %v", err)
	} else {
		conn.Close()
		result.Status = "ok"
		result.Message = "Connection successful"
	}

	result.Duration = time.Since(start)
	return result
}

// validateDocling checks Docling API health
func (v *Validator) validateDocling() ValidationResult {
	start := time.Now()

	result := ValidationResult{
		Component: "docling",
		Status:    "pending",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	url := v.cfg.DoclingURL + "/openapi.json"
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		result.Status = "error"
		result.Message = fmt.Sprintf("Failed to create request: %v", err)
		result.Duration = time.Since(start)
		return result
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		result.Status = "error"
		result.Message = fmt.Sprintf("Failed to connect: %v", err)
	} else {
		defer resp.Body.Close()
		if resp.StatusCode == http.StatusOK {
			result.Status = "ok"
			result.Message = "Health check passed"
		} else {
			result.Status = "warning"
			result.Message = fmt.Sprintf("Health check returned status %d", resp.StatusCode)
		}
	}

	result.Duration = time.Since(start)
	return result
}

// validateResourceManager checks Resource Manager API health
func (v *Validator) validateResourceManager() ValidationResult {
	start := time.Now()

	result := ValidationResult{
		Component: "resource-manager",
		Status:    "pending",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	url := v.cfg.ResourceManagerURL + "/health"
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		result.Status = "warning"
		result.Message = fmt.Sprintf("Failed to create request (non-critical): %v", err)
		result.Duration = time.Since(start)
		return result
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		result.Status = "warning"
		result.Message = fmt.Sprintf("Failed to connect (non-critical): %v", err)
	} else {
		defer resp.Body.Close()
		if resp.StatusCode == http.StatusOK {
			result.Status = "ok"
			result.Message = "Health check passed"
		} else {
			result.Status = "warning"
			result.Message = fmt.Sprintf("Health check returned status %d", resp.StatusCode)
		}
	}

	result.Duration = time.Since(start)
	return result
}

// ValidateStartup performs quick startup validation
func ValidateStartup() error {
	cfg, err := Load()
	if err != nil {
		return fmt.Errorf("failed to load configuration: %w", err)
	}

	validator := NewValidator(cfg)
	results := validator.ValidateAll()

	hasErrors := false
	for _, r := range results {
		switch r.Status {
		case "ok":
			fmt.Printf("✅ %s: %s (%v)\n", r.Component, r.Message, r.Duration)
		case "warning":
			fmt.Printf("⚠️  %s: %s (%v)\n", r.Component, r.Message, r.Duration)
		case "error":
			fmt.Printf("❌ %s: %s (%v)\n", r.Component, r.Message, r.Duration)
			hasErrors = true
		}
	}

	if hasErrors {
		return fmt.Errorf("startup validation failed with errors")
	}

	return nil
}

// extractHostPort extracts host and port from URL
func extractHostPort(url string) string {
	// Handle different URL formats
	switch {
	case len(url) >= 8 && url[:8] == "redis://":
		return url[8:]
	case len(url) >= 10 && url[:10] == "rediss://":
		return url[10:]
	case len(url) >= 7 && url[:7] == "amqp://":
		return url[7:]
	default:
		return url
	}
}

// LoadWithValidation loads configuration and validates connectivity
func LoadWithValidation(allowWarnings bool) (*Config, error) {
	cfg, err := Load()
	if err != nil {
		return nil, fmt.Errorf("failed to load configuration: %w", err)
	}

	validator := NewValidator(cfg)
	results := validator.ValidateAll()

	hasErrors := false
	for _, r := range results {
		switch r.Status {
		case "ok":
			fmt.Printf("✅ %s: %s (%v)\n", r.Component, r.Message, r.Duration)
		case "warning":
			if !allowWarnings {
				fmt.Printf("⚠️  %s: %s (%v)\n", r.Component, r.Message, r.Duration)
			}
		case "error":
			fmt.Printf("❌ %s: %s (%v)\n", r.Component, r.Message, r.Duration)
			hasErrors = true
		}
	}

	if hasErrors {
		return nil, fmt.Errorf("configuration validation failed")
	}

	return cfg, nil
}
