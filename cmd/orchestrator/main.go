package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/rs/zerolog"
	"golang.org/x/sync/errgroup"
	"ia-text-orchestrator/internal/broker"
	"ia-text-orchestrator/internal/config"
	"ia-text-orchestrator/internal/events"
	"ia-text-orchestrator/internal/health"
	"ia-text-orchestrator/internal/models"
	redisclient "ia-text-orchestrator/internal/redis"
	"ia-text-orchestrator/pkg/logging"
	"ia-text-orchestrator/pkg/metrics"
)

var (
	cfg      *config.Config
	mqBroker *broker.RabbitMQBroker
	redis    *redisclient.RedisClient
	eventBus *events.EventBus
	logger   zerolog.Logger
)

func main() {
	var err error

	logging.Init("info")
	logger = logging.GetLogger()

	cfg, err = config.Load()
	if err != nil {
		logger.Fatal().Msgf("Failed to load configuration: %v", err)
	}

	logger.Info().Msg("Starting IA Text Orchestrator")

	// Initialize metrics
	metrics.Init()

	// Start metrics collector for runtime stats
	metrics.StartMetricsCollector()
	logger.Info().Msg("Started runtime metrics collector")

	mqBroker, err = broker.New(cfg)
	if err != nil {
		logger.Fatal().Msgf("Failed to connect to RabbitMQ: %v", err)
	}
	defer mqBroker.Close()

	redis, err = redisclient.New(cfg)
	if err != nil {
		logger.Fatal().Msgf("Failed to connect to Redis: %v", err)
	}
	defer redis.Close()

	// Initialize EventBus with Redis client
	eventBus = events.NewEventBus(redis.GetClient())

	r := setupRouter()

	addr := fmt.Sprintf(":%d", cfg.HTTPPort)
	logger.Info().Msgf("Server starting on %s", addr)

	srv := &http.Server{
		Addr:    addr,
		Handler: r,
		// Add timeouts to prevent resource exhaustion
		ReadTimeout:    15 * time.Second,
		WriteTimeout:   30 * time.Second,
		IdleTimeout:    120 * time.Second,
		MaxHeaderBytes: 1 << 20, // 1MB
	}

	// Create context for graceful shutdown
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Use errgroup to manage goroutines
	g, gCtx := errgroup.WithContext(ctx)

	// Start HTTP server
	g.Go(func() error {
		logger.Info().Msgf("HTTP server listening on %s", addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			return fmt.Errorf("HTTP server error: %w", err)
		}
		return nil
	})

	// Start queue metrics updater
	g.Go(func() error {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()

		logger.Info().Msg("Queue metrics updater started")

		for {
			select {
			case <-gCtx.Done():
				logger.Info().Msg("Queue metrics updater stopped")
				return nil
			case <-ticker.C:
				if err := mqBroker.UpdateQueueMetrics(); err != nil {
					logger.Warn().Err(err).Msg("Failed to update queue metrics")
				}
			}
		}
	})

	// Handle shutdown signals
	g.Go(func() error {
		quit := make(chan os.Signal, 1)
		signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)

		select {
		case sig := <-quit:
			logger.Info().Msgf("Received signal %v, shutting down...", sig)
		case <-gCtx.Done():
			logger.Info().Msg("Context cancelled, shutting down...")
		}

		// Cancel main context to stop all goroutines
		cancel()

		// Shutdown HTTP server with timeout
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer shutdownCancel()

		if err := srv.Shutdown(shutdownCtx); err != nil {
			logger.Error().Err(err).Msg("Server forced to shutdown")
			return err
		}

		logger.Info().Msg("Server stopped gracefully")
		return nil
	})

	// Wait for all goroutines to complete
	if err := g.Wait(); err != nil {
		logger.Error().Err(err).Msg("Application error")
		os.Exit(1)
	}

	logger.Info().Msg("Application shutdown complete")
}

func setupRouter() *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(ginLogger())
	r.Use(metricsMiddleware())

	r.GET("/health", healthHandler)
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	v1 := r.Group("/v1")
	{
		v1.POST("/documents/process", createJobHandler)
		v1.GET("/documents/:id", getJobHandler)
		v1.DELETE("/documents/:id", deleteJobHandler)
	}

	return r
}

func ginLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		path := c.Request.URL.Path
		query := c.Request.URL.RawQuery

		c.Next()

		latency := time.Since(start)
		status := c.Writer.Status()

		logger.Info().
			Str("method", c.Request.Method).
			Str("path", path).
			Str("query", query).
			Int("status", status).
			Dur("latency", latency).
			Str("ip", c.ClientIP()).
			Msg("HTTP Request")
	}
}

func metricsMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()

		c.Next()

		duration := time.Since(start).Seconds()
		status := strconv.Itoa(c.Writer.Status())

		metrics.HTTPRequestsTotal.WithLabelValues(
			c.Request.Method,
			c.FullPath(),
			status,
		).Inc()

		metrics.HTTPLatencySeconds.WithLabelValues(
			c.Request.Method,
			c.FullPath(),
		).Observe(duration)
	}
}

func healthHandler(c *gin.Context) {
	redisStatus := "ok"
	if err := redis.HealthCheck(); err != nil {
		redisStatus = err.Error()
	}

	brokerStatus := "ok"
	if err := mqBroker.HealthCheck(); err != nil {
		brokerStatus = err.Error()
	}

	status := "healthy"
	httpStatus := http.StatusOK
	if redisStatus != "ok" || brokerStatus != "ok" {
		status = "unhealthy"
		httpStatus = http.StatusServiceUnavailable
	}

	c.JSON(httpStatus, gin.H{
		"status":  status,
		"redis":   redisStatus,
		"broker":  brokerStatus,
		"service": "orchestrator",
	})
}

func createJobHandler(c *gin.Context) {
	start := time.Now()

	var req models.CreateJobRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_request",
			Detail: err.Error(),
		})
		return
	}

	if req.DocumentBase64 == "" && req.DocumentURL == "" {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_request",
			Detail: "either document_base64 or document_url is required",
		})
		return
	}

	// Validate input to prevent DoS and SSRF attacks
	if err := validateDocumentInput(&req); err != nil {
		logger.Warn().Err(err).Msg("Document input validation failed")
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_input",
			Detail: err.Error(),
		})
		return
	}

	jobID := generateJobID()

	ctx := c.Request.Context()

	if err := redis.SetJobStatus(ctx, jobID, models.StatusPending); err != nil {
		logger.Error().Msgf("Failed to set job status: %v", err)
		metrics.JobsTotal.WithLabelValues("failed", "create").Inc()
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to create job",
		})
		return
	}

	if err := redis.SetJobCreated(ctx, jobID); err != nil {
		logger.Error().Msgf("Failed to set job created time: %v", err)
	}

	// Increment jobs in progress
	metrics.JobsInProgress.Inc()
	metrics.JobsTotal.WithLabelValues("created", "document").Inc()

	// Publish JobCreated event
	if err := eventBus.PublishJobCreated(ctx, jobID); err != nil {
		logger.Warn().Err(err).Msg("Failed to publish JobCreated event")
	}

	jobMsg := &models.JobMessage{
		JobID:          jobID,
		DocumentBase64: req.DocumentBase64,
		DocumentURL:    req.DocumentURL,
	}

	if err := mqBroker.PublishJobMessage(ctx, jobMsg); err != nil {
		logger.Error().Msgf("Failed to publish job message: %v", err)
		redis.SetJobStatus(ctx, jobID, models.StatusFailed)
		metrics.JobsInProgress.Dec()
		metrics.JobsTotal.WithLabelValues("failed", "publish").Inc()
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to queue job",
		})
		return
	}

	// Track publish to queue
	metrics.QueuePublishTotal.WithLabelValues("extract").Inc()

	logger.Info().Msgf("Job created: %s", jobID)

	// Track job creation duration
	metrics.JobDurationSeconds.WithLabelValues("creation").Observe(time.Since(start).Seconds())

	c.JSON(http.StatusAccepted, models.CreateJobResponse{
		JobID:     jobID,
		Status:    models.StatusPending,
		StatusURL: fmt.Sprintf("/v1/documents/%s", jobID),
	})
}

func getJobHandler(c *gin.Context) {
	jobID := c.Param("id")
	ctx := c.Request.Context()

	status, err := redis.GetJobStatus(ctx, jobID)
	if err != nil {
		logger.Warn().Msgf("Job not found: %s", jobID)
		c.JSON(http.StatusNotFound, models.ErrorResponse{
			Error: "job_not_found",
		})
		return
	}

	results, _ := redis.GetJobResults(ctx, jobID)
	errorMsg, _ := redis.GetJobError(ctx, jobID)

	c.JSON(http.StatusOK, models.GetJobResponse{
		JobID:   jobID,
		Status:  status,
		Results: results,
		Error:   errorMsg,
	})
}

func deleteJobHandler(c *gin.Context) {
	jobID := c.Param("id")
	ctx := c.Request.Context()

	status, err := redis.GetJobStatus(ctx, jobID)
	if err != nil {
		c.JSON(http.StatusNotFound, models.ErrorResponse{
			Error: "job_not_found",
		})
		return
	}

	if status == models.StatusCompleted || status == models.StatusFailed {
		if err := redis.DeleteJob(ctx, jobID); err != nil {
			logger.Error().Msgf("Failed to delete job: %v", err)
		}
		c.JSON(http.StatusOK, gin.H{
			"message": "job deleted",
			"job_id":  jobID,
		})
		return
	}

	c.JSON(http.StatusConflict, models.ErrorResponse{
		Error:  "job_in_progress",
		Detail: "cannot delete job that is still processing",
	})
}

func generateJobID() string {
	return fmt.Sprintf("%d", time.Now().UnixNano())
}

// validateDocumentInput validates the document input to prevent DoS and SSRF attacks
func validateDocumentInput(req *models.CreateJobRequest) error {
	const (
		MaxDocumentSize = 10 * 1024 * 1024 // 10MB
		MaxURLLength    = 2048
	)

	// Validate DocumentBase64 size
	if req.DocumentBase64 != "" {
		// Decode to check actual size
		decoded, err := base64.StdEncoding.DecodeString(req.DocumentBase64)
		if err != nil {
			return fmt.Errorf("invalid base64 encoding")
		}

		if len(decoded) > MaxDocumentSize {
			return fmt.Errorf("document too large: %d bytes (max %d bytes)", len(decoded), MaxDocumentSize)
		}
	}

	// Validate DocumentURL
	if req.DocumentURL != "" {
		// Check URL length
		if len(req.DocumentURL) > MaxURLLength {
			return fmt.Errorf("URL too long: %d characters (max %d)", len(req.DocumentURL), MaxURLLength)
		}

		// Parse URL
		u, err := url.Parse(req.DocumentURL)
		if err != nil {
			return fmt.Errorf("invalid URL format: %w", err)
		}

		// Whitelist allowed schemes
		scheme := strings.ToLower(u.Scheme)
		if scheme != "http" && scheme != "https" {
			return fmt.Errorf("URL scheme not allowed: %s (only http and https are permitted)", u.Scheme)
		}

		// Get hostname
		hostname := u.Hostname()
		if hostname == "" {
			return fmt.Errorf("URL must have a valid hostname")
		}

		// Block localhost and loopback addresses
		if hostname == "localhost" || hostname == "127.0.0.1" || hostname == "::1" {
			return fmt.Errorf("localhost URLs are not allowed")
		}

		// Block cloud metadata endpoints (SSRF prevention)
		blockedHosts := []string{
			"169.254.169.254",          // AWS, Azure, GCP metadata
			"metadata.google.internal", // GCP metadata
			"169.254.169.254",          // Azure metadata
			"metadata",
		}
		for _, blocked := range blockedHosts {
			if hostname == blocked || strings.HasSuffix(hostname, "."+blocked) {
				return fmt.Errorf("access to metadata services is not allowed")
			}
		}

		// Check if hostname is an IP address
		if ip := net.ParseIP(hostname); ip != nil {
			// Block private IP ranges (RFC 1918)
			if ip.IsLoopback() {
				return fmt.Errorf("loopback IP addresses are not allowed: %s", ip.String())
			}
			if ip.IsPrivate() {
				return fmt.Errorf("private IP addresses are not allowed: %s", ip.String())
			}
			// Block link-local addresses
			if ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() {
				return fmt.Errorf("link-local IP addresses are not allowed: %s", ip.String())
			}
		}

		// Additional check: resolve hostname to ensure it doesn't resolve to private IPs
		// This prevents DNS rebinding attacks
		ips, err := net.LookupIP(hostname)
		if err != nil {
			return fmt.Errorf("failed to resolve hostname: %w", err)
		}

		for _, ip := range ips {
			if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() {
				return fmt.Errorf("hostname resolves to a private/loopback IP address: %s -> %s", hostname, ip.String())
			}
		}
	}

	return nil
}
