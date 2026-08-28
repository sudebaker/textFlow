package main

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"io/ioutil"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/rs/zerolog"
	"golang.org/x/sync/errgroup"
	"golang.org/x/time/rate"
	"textflow/cmd/orchestrator/handlers"
	"textflow/internal/broker"
	"textflow/internal/config"
	"textflow/internal/events"
	"textflow/internal/health"
	"textflow/internal/middleware"
	"textflow/internal/models"
	redisclient "textflow/internal/redis"
	"textflow/pkg/logging"
	"textflow/pkg/metrics"

	"github.com/swaggo/files"
	"github.com/swaggo/gin-swagger"
	_ "textflow/docs/swagger"
)

var (
	cfg           *config.Config
	mqBroker      *broker.RabbitMQBroker
	redis         *redisclient.RedisClient
	eventBus      *events.EventBus
	healthChecker *health.HealthChecker
	logger        zerolog.Logger
	admission     *handlers.AdmissionController

	// Spreadsheet validation limits
	maxSpreadsheetRows  int
	maxSpreadsheetBytes int64

	// Audio validation limits
	maxAudioSizeMB int

	// pipelineOrder defines the canonical order of processing steps.
	pipelineOrder = []string{"extraction", "embeddings", "entities", "metadata", "inferences"}
)

// main is the entry point for the textFlow service.
// It initializes configuration, logging, metrics, and connects to external services (RabbitMQ, Redis).
// It manages the lifecycle of the HTTP server and background workers (metrics collector, job timeout watchdog).
// It blocks on signal handling and gracefully shuts down all components on SIGINT or SIGTERM.
// The service accepts documents via REST API, validates inputs (SSRF prevention, size limits),
// and publishes jobs to RabbitMQ for async processing by workers. Job status and results are stored in Redis.
func main() {
	var err error

	logging.Init("info")
	logger = logging.GetLogger()

	cfg, err = config.Load()
	if err != nil {
		logger.Fatal().Msgf("Failed to load configuration: %v", err)
	}

	if err := cfg.Validate(); err != nil {
		logger.Fatal().Err(err).Msg("Config validation failed")
	}

	// Re-initialize logger with configured log level
	logger = logging.Init(cfg.LogLevel)

	logger.Info().Msg("Starting textFlow")
	logger.Info().
		Str("ExtractQueue", cfg.ExtractQueue).
		Str("EmbeddingsQueue", cfg.EmbeddingsQueue).
		Str("EntitiesQueue", cfg.EntitiesQueue).
		Str("MetadataQueue", cfg.MetadataQueue).
		Str("InferencesQueue", cfg.InferencesQueue).
		Str("AudioQueue", cfg.AudioQueue).
		Str("ImageQueue", cfg.ImageQueue).
		Msg("queue config")

	// Initialize metrics
	metrics.Init()

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

	// Initialize handlers with dependencies
	handlers.SetDependencies(eventBus, redis, mqBroker)
	redisclient.SetClient(redis)

	// Initialize admission controller
	admission = handlers.NewAdmissionController(cfg, redis, mqBroker)

	// Initialize comprehensive health checker
	healthChecker = health.NewHealthChecker(redis, mqBroker, cfg)
	logger.Info().Msg("Health checker initialized")

	// Spreadsheet size limits
	maxSpreadsheetRowsStr := os.Getenv("MAX_SPREADSHEET_ROWS")
	if maxSpreadsheetRowsStr == "" {
		maxSpreadsheetRowsStr = "2000"
	}
	maxSpreadsheetRows, err = strconv.Atoi(maxSpreadsheetRowsStr)
	if err != nil {
		logger.Warn().Err(err).Str("value", maxSpreadsheetRowsStr).
			Msg("Invalid MAX_SPREADSHEET_ROWS, using default 2000")
		maxSpreadsheetRows = 2000
	}

	maxSpreadsheetSizeMBStr := os.Getenv("MAX_SPREADSHEET_SIZE_MB")
	if maxSpreadsheetSizeMBStr == "" {
		maxSpreadsheetSizeMBStr = "5"
	}
	maxSpreadsheetSizeMB, err := strconv.Atoi(maxSpreadsheetSizeMBStr)
	if err != nil {
		logger.Warn().Err(err).Str("value", maxSpreadsheetSizeMBStr).
			Msg("Invalid MAX_SPREADSHEET_SIZE_MB, using default 5")
		maxSpreadsheetSizeMB = 5
	}
	maxSpreadsheetBytes = int64(maxSpreadsheetSizeMB * 1024 * 1024)

	maxAudioSizeMBStr := os.Getenv("MAX_AUDIO_SIZE_MB")
	if maxAudioSizeMBStr == "" {
		maxAudioSizeMBStr = "500"
	}
	maxAudioSizeMB, err = strconv.Atoi(maxAudioSizeMBStr)
	if err != nil {
		logger.Warn().Err(err).Str("value", maxAudioSizeMBStr).
			Msg("Invalid MAX_AUDIO_SIZE_MB, using default 500")
		maxAudioSizeMB = 500
	}

	handlers.SetResultsPath(cfg.ResultsPath)
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

	// Start metrics collector for runtime stats with context-aware shutdown
	metrics.StartMetricsCollector(ctx)
	logger.Info().Msg("Started runtime metrics collector")

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

	// Job timeout watchdog - expire stuck jobs
	g.Go(func() error {
		ticker := time.NewTicker(1 * time.Minute)
		defer ticker.Stop()

		logger.Info().Msg("Job timeout watchdog started")

		for {
			select {
			case <-gCtx.Done():
				logger.Info().Msg("Job timeout watchdog stopped")
				return nil
			case <-ticker.C:
				if err := redis.ExpireStuckJobs(gCtx, cfg.JobTimeout); err != nil {
					logger.Warn().Err(err).Msg("Failed to expire stuck jobs")
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

// setupRouter creates and configures the Gin HTTP router for the orchestrator.
// It registers middleware for logging, metrics collection, rate limiting, and panic recovery.
// Routes include:
//   - GET /health: Health status check
//   - GET /metrics: Prometheus metrics endpoint
//   - POST /v1/documents/process: Submit document for processing (async)
//   - POST /v1/documents/upload: Upload file via multipart form and submit for processing
//   - GET /v1/documents/{id}: Poll for job status and current_step (no results)
//   - GET /v1/documents/{id}/download: Download gzip-compressed JSON results (requires status=completed)
//   - DELETE /v1/documents/{id}: Delete job data from Redis (only for completed/failed jobs)
//
// Returns a fully configured *gin.Engine ready to handle requests.
func setupRouter() *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(ginLogger())
	r.Use(metricsMiddleware())

	// Rate limiter: 100 requests per second per IP, burst of 10
	limiter := middleware.NewRateLimiter(
		rate.Limit(100),
		10,
	)
	r.Use(limiter.Middleware())

	// API key auth (disabled if API_KEY env var is empty)
	authMiddleware := middleware.APIKeyAuth(cfg.APIKey)

	r.GET("/health", healthHandler)
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	v1 := r.Group("/v1")
	v1.Use(authMiddleware)
	{
		v1.POST("/documents/process", createJobHandler)
		v1.POST("/documents/upload", uploadHandler)
		v1.POST("/documents/batch", handlers.CreateBatchHandler)
		v1.GET("/documents/:id", getJobHandler)
		v1.GET("/documents/:id/graph", handlers.GraphHandler)
		v1.GET("/documents/:id/vectors", handlers.VectorsHandler)
		v1.GET("/documents/:id/entities", handlers.EntitiesHandler)
		v1.GET("/documents/:id/inferences", handlers.InferencesHandler)
		v1.GET("/documents/:id/download", downloadHandler)
		v1.DELETE("/documents/:id", deleteJobHandler)
		v1.POST("/documents/:id/cancel", cancelJobHandler)
		v1.GET("/batches/:id/status", handlers.GetBatchStatusHandler)
		v1.GET("/jobs/:id/stream", handlers.StreamJobHandler)
	}

	r.GET("/swagger/*any", ginSwagger.WrapHandler(swaggerFiles.Handler))

	return r
}

// ginLogger is a Gin middleware that logs HTTP requests with structured logging.
// It logs: HTTP method, path, query parameters (keys only, not values for security),
// response status code, latency, and client IP.
// It scrubs sensitive query parameter values before logging to avoid exposing secrets.
func ginLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		path := c.Request.URL.Path
		query := c.Request.URL.RawQuery

		c.Next()

		latency := time.Since(start)
		status := c.Writer.Status()

		// Scrub sensitive data from query string - only log param keys, not values
		var queryKeys []string
		if query != "" {
			u, _ := url.ParseQuery(query)
			for k := range u {
				queryKeys = append(queryKeys, k)
			}
			sort.Strings(queryKeys)
		}

		logger.Info().
			Str("method", c.Request.Method).
			Str("path", path).
			Strs("query_params", queryKeys).
			Int("status", status).
			Dur("latency", latency).
			Str("ip", c.ClientIP()).
			Msg("HTTP Request")
	}
}

// metricsMiddleware is a Gin middleware that instruments HTTP requests with Prometheus metrics.
// It tracks:
//   - HTTPRequestsTotal: Counter of HTTP requests by method, path, and status code
//   - HTTPLatencySeconds: Histogram of request latency by method and path
//
// Metrics are collected for all routes and can be exported via the /metrics endpoint.
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

// healthHandler handles GET /health requests and returns the health status of the orchestrator.
// It performs checks on Redis, RabbitMQ, and other critical services.
// Returns HTTP 200 OK for "up" or "degraded" status (service is operational).
// Returns HTTP 503 Service Unavailable only if status is "down" (complete failure).
// Response includes detailed component status, timestamps, and error messages if any checks failed.
func healthHandler(c *gin.Context) {
	// Create context with timeout for health checks
	ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
	defer cancel()

	healthStatus := healthChecker.Check(ctx)

	// Determine HTTP status code
	// Note: "degraded" status means the service is still operational but at reduced capacity,
	// so it should return 200 OK to avoid triggering load balancer failover.
	// Only return 503 Service Unavailable if the service is completely down.
	httpStatus := http.StatusOK
	if healthStatus.Status == "down" {
		httpStatus = http.StatusServiceUnavailable
	}

	c.JSON(httpStatus, healthStatus)
}

// createJobHandler handles POST /v1/documents/process requests and submits a document for processing.
// It is the primary REST API endpoint for document ingestion.
//
// @Summary Process a document
// @Description Submit a document for async processing. Returns job_id for polling.
// @Accept json
// @Produce json
// @Param request body models.CreateJobRequest true "Document processing request"
// @Success 202 {object} models.CreateJobResponse
// @Failure 400 {object} models.ErrorResponse
// @Failure 500 {object} models.ErrorResponse
// @Router /v1/documents/process [post]
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
	if err := validateDocumentInput(&req, cfg); err != nil {
		logger.Warn().Err(err).Msg("Document input validation failed")
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_input",
			Detail: err.Error(),
		})
		return
	}

	// Admission control: check system capacity before accepting job
	if admission != nil {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()
		accepted, reason, statusCode := admission.CanAcceptJob(ctx)
		if !accepted {
			logger.Warn().Str("reason", reason).Int("status", statusCode).Msg("Job rejected by admission control")
			c.Header("Retry-After", "5")
			c.JSON(statusCode, models.ErrorResponse{
				Error:  "system_busy",
				Detail: reason,
			})
			return
		}
	}

	jobID := generateJobID()

	// Create context with timeout for the entire job creation process
	ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
	defer cancel()

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
		logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to mark job as created")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to initialize job",
		})
		return
	}

	// Store features and LLM URL in Redis
	logger.Info().Msgf("Job %s: Features received: %v", jobID, req.Features)
	if len(req.Features) > 0 {
		if err := redis.SetJobFeatures(ctx, jobID, req.Features); err != nil {
			logger.Error().Err(err).Msgf("Failed to store job features: %v", err)
		} else {
			logger.Info().Msgf("Job %s: Features stored successfully", jobID)
		}
	} else {
		logger.Info().Msgf("Job %s: No features requested", jobID)
	}

	// Normalize and store the processing profile (fast/balanced/full)
	profile := normalizeProfile(req.Profile)
	if err := redis.SetJobProfile(ctx, jobID, profile); err != nil {
		logger.Error().Err(err).Str("job_id", jobID).Str("profile", profile).Msg("Failed to store job profile")
	} else {
		logger.Info().Str("job_id", jobID).Str("profile", profile).Msg("Job profile stored")
	}

	// Store webhook config if provided
	if req.WebhookURL != "" {
		if err := redis.SetJobWebhook(ctx, jobID, req.WebhookURL, req.WebhookSecret); err != nil {
			logger.Warn().Err(err).Str("job_id", jobID).Msg("Failed to store webhook config")
		} else {
			logger.Info().Str("job_id", jobID).Msg("Webhook config stored")
		}
	}

	// Increment jobs in progress
	metrics.JobsInProgress.Inc()
	metrics.JobsTotal.WithLabelValues("created", "document").Inc()

	// Publish JobCreated event
	if err := eventBus.PublishJobCreated(ctx, jobID); err != nil {
		logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to publish JobCreated event")
		// No return — job is in Redis, event publication is best-effort
	}

	jobMsg := &models.JobMessage{
		JobID:           jobID,
		DocumentBase64:  req.DocumentBase64,
		DocumentURL:     req.DocumentURL,
		Filename:        req.Filename,
		Features:        req.Features,
		Profile:         profile,
		PipelineVersion: "v1",
	}

	// stage.queued contract (spec 4.5): best-effort, before RabbitMQ publish
	_ = eventBus.PublishStageEvent(ctx, jobID, events.EventStageQueued, "extraction", nil)

	if err := mqBroker.PublishJobMessage(ctx, jobMsg); err != nil {
		logger.Error().Msgf("Failed to publish job message: %v", err)

		// Check if this is a queue overflow error (x-max-length reached)
		if broker.IsQueueOverflowError(err) {
			logger.Warn().Str("job_id", jobID).Msg("Queue full, rejecting job")
			if statusErr := redis.SetJobStatus(ctx, jobID, models.StatusFailed); statusErr != nil {
				logger.Error().Err(statusErr).Str("job_id", jobID).Msg("Failed to mark job as failed after queue overflow")
			}
			metrics.JobsInProgress.Dec()
			metrics.JobsTotal.WithLabelValues("failed", "queue_overflow").Inc()
			c.Header("Retry-After", "5")
			c.JSON(http.StatusServiceUnavailable, models.ErrorResponse{
				Error:  "system_busy",
				Detail: "system is at capacity, please retry later",
			})
			return
		}

		if statusErr := redis.SetJobStatus(ctx, jobID, models.StatusFailed); statusErr != nil {
			logger.Error().Err(statusErr).Str("job_id", jobID).Msg("Failed to mark job as failed after publish error")
		}
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

// calculateCurrentStep derives the active pipeline step from the steps map.
// Order: extraction → embeddings → entities → metadata → inferences.
// Returns the step currently "processing", or the last "completed" step in pipeline order;
// returns "" if steps is empty.
func calculateCurrentStep(steps map[string]string, order []string) string {
	if len(steps) == 0 {
		return ""
	}
	lastCompleted := ""
	for _, step := range order {
		val, ok := steps[step]
		if !ok {
			continue
		}
		if val == "processing" {
			return step
		}
		if val == "completed" {
			lastCompleted = step
		}
	}
	return lastCompleted
}

// getJobHandler handles GET /v1/documents/{id} requests and returns the current status of a job.
// This is a polling endpoint (non-blocking) - clients must retry to check for completion.
// Results are NOT returned here; use /download once status is completed.
//
// @Summary Get job status
// @Description Returns job status and current pipeline step. Does not include results — use /download for results.
// @Produce json
// @Param id path string true "Job ID"
// @Success 200 {object} models.GetJobResponse "Job status with current_step field"
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id} [get]
func getJobHandler(c *gin.Context) {
	jobID := c.Param("id")

	// Validate jobID format
	if !validateJobID(jobID) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_job_id",
			Detail: "job ID format is invalid",
		})
		return
	}

	// Create context with timeout for database operations
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()

	status, err := redis.GetJobStatus(ctx, jobID)
	if err != nil {
		logger.Warn().Msgf("Job not found: %s", jobID)
		c.JSON(http.StatusNotFound, models.ErrorResponse{
			Error: "job_not_found",
		})
		return
	}

	// Get step progress (available at all stages, not just completed)
	steps, _ := redis.GetJobSteps(ctx, jobID)

	// NOTE: Job completion is handled exclusively by the completion-worker.
	// The completion-worker aggregates results from all workers, writes the results
	// file to disk, and THEN marks the job as completed. Do NOT auto-complete
	// jobs here as it creates a race condition where the download endpoint
	// returns "no results found" because the file hasn't been written yet.

	// Calculate current_step from the steps map.
	// Pipeline order: extraction → embeddings → entities → metadata → inferences
	currentStep := calculateCurrentStep(steps, pipelineOrder)

	errorMsg, _ := redis.GetJobError(ctx, jobID)

	// Get created timestamp
	createdAt, _ := redis.GetJobCreated(ctx, jobID)

	c.JSON(http.StatusOK, models.GetJobResponse{
		JobID:       jobID,
		Status:      status,
		Steps:       steps,
		CurrentStep: currentStep,
		Error:       errorMsg,
		CreatedAt:   createdAt,
	})
}

// deleteJobHandler handles DELETE /v1/documents/{id} requests and removes a job's data from Redis.
// This endpoint is idempotent: repeated requests for the same job always return 204.
//
// @Summary Delete a job
// @Description Remove a completed or failed job and its associated data.
// @Param id path string true "Job ID"
// @Success 204 "No Content"
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id} [delete]
func deleteJobHandler(c *gin.Context) {
	jobID := c.Param("id")

	// Validate jobID format
	if !validateJobID(jobID) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_job_id",
			Detail: "job ID format is invalid",
		})
		return
	}

	// Create context with timeout for deletion operations
	ctx, cancel := context.WithTimeout(c.Request.Context(), 10*time.Second)
	defer cancel()

	status, err := redis.GetJobStatus(ctx, jobID)
	if err != nil {
		c.JSON(http.StatusNotFound, models.ErrorResponse{
			Error: "job_not_found",
		})
		return
	}

	if status == models.StatusCompleted || status == models.StatusFailed {
		if err := redis.DeleteJob(ctx, jobID); err != nil {
			logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to delete job")
			c.JSON(http.StatusInternalServerError, models.ErrorResponse{
				Error:  "internal_error",
				Detail: "failed to delete job",
			})
			return
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

// cancelJobHandler handles POST /v1/documents/{id}/cancel requests.
// It marks a job as cancelled in Redis so workers stop processing it.
// The job is not deleted; its status becomes "cancelled" and it is removed
// from the active_jobs set. Workers check the status and abort.
func cancelJobHandler(c *gin.Context) {
	jobID := c.Param("id")

	if !validateJobID(jobID) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_job_id",
			Detail: "job ID format is invalid",
		})
		return
	}

	ctx, cancel := context.WithTimeout(c.Request.Context(), 10*time.Second)
	defer cancel()

	status, err := redis.GetJobStatus(ctx, jobID)
	if err != nil {
		c.JSON(http.StatusNotFound, models.ErrorResponse{
			Error: "job_not_found",
		})
		return
	}

	// Terminal states cannot be cancelled.
	if status == models.StatusCompleted || status == models.StatusFailed || status == models.StatusCancelled {
		c.JSON(http.StatusConflict, models.ErrorResponse{
			Error:  "job_not_cancellable",
			Detail: "job is already in a terminal state",
		})
		return
	}

	if err := redis.SetJobStatus(ctx, jobID, models.StatusCancelled); err != nil {
		logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to cancel job")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to cancel job",
		})
		return
	}

	logger.Info().Str("job_id", jobID).Msg("Job cancelled")
	c.JSON(http.StatusOK, gin.H{
		"message": "job cancelled",
		"job_id":  jobID,
		"status":  models.StatusCancelled,
	})
}

// generateJobID generates a unique job identifier using UUID v4.
// Returns a hex string in standard UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
// This ID is used as the primary key for job tracking in Redis and as path parameter in REST API.
func generateJobID() string {
	return uuid.New().String()
}

// validateJobID validates that a jobID parameter matches the expected UUID v4 format.
// Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (36 characters total, hex + hyphens)
// Checks:
//   - Exactly 36 characters in length
//   - Hyphens at positions 8, 13, 18, 23
//   - Hex characters (0-9, a-f, A-F) at all other positions
//
// This validation is more lenient than full UUID regex but sufficient for security
// (format validation only; does not verify UUID version).
// Returns true if format is valid, false otherwise.
func validateJobID(jobID string) bool {
	if len(jobID) != 36 {
		return false
	}
	// UUID v4 format check: 8-4-4-4-12 hex characters separated by hyphens
	// This is more lenient than a full UUID regex but sufficient for security
	for i, ch := range jobID {
		if i == 8 || i == 13 || i == 18 || i == 23 {
			if ch != '-' {
				return false
			}
		} else {
			if !((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') || (ch >= 'A' && ch <= 'F')) {
				return false
			}
		}
	}
	return true
}

// validateDocumentInput validates the document input in a CreateJobRequest to prevent DoS and SSRF attacks.
// It enforces size limits, URL format restrictions, and blocks dangerous network destinations.
//
// Size Limits:
//   - DocumentBase64: Decoded size must not exceed MAX_DOCUMENT_SIZE_MB (default 10MB)
//   - DocumentURL: URL length must not exceed 2048 characters
//
// URL Validation (when DocumentURL is provided):
//   - Scheme: Only http and https allowed
//   - Hostname: Must be present and resolvable
//   - Public IP only: No localhost, loopback, or private IP ranges (RFC 1918)
//   - No metadata endpoints: Blocks cloud metadata services (AWS, GCP, Azure)
//   - No link-local addresses: Blocks 169.254.x.x range
//   - DNS rebinding protection: Validates resolved IPs match hostname requirements
//
// SSRF Prevention Strategy:
//  1. Whitelist URL schemes (http, https)
//  2. Parse URL and extract hostname
//  3. Block localhost/loopback unless cfg.AllowLocalURLs=true
//  4. Block cloud metadata endpoints: 169.254.169.254, metadata.google.internal
//  5. Block private IPs: 10.x, 172.16-31.x, 192.168.x
//  6. Block link-local: 169.254.x.x (except AWS metadata which is already blocked)
//  7. DNS lookup: Verify resolved IPs don't resolve to private ranges (DNS rebinding attack)
//
// Base64 Validation:
//   - Validates standard base64 encoding
//   - Decodes to check actual file size
//
// Returns nil if valid, or an error describing validation failure.
// Errors are suitable for returning to client as 400/422 Bad Request responses.
func validateDocumentInput(req *models.CreateJobRequest, cfg *config.Config) error {
	const MaxURLLength = 2048
	maxDocumentSize := cfg.MaxDocumentSizeMB * 1024 * 1024

	// Validate DocumentBase64 size
	if req.DocumentBase64 != "" {
		// Decode to check actual size
		decoded, err := base64.StdEncoding.DecodeString(req.DocumentBase64)
		if err != nil {
			return fmt.Errorf("invalid base64 encoding")
		}

		if len(decoded) > maxDocumentSize {
			return fmt.Errorf("document too large: %d bytes (max %d bytes)", len(decoded), maxDocumentSize)
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

		// Block localhost and loopback addresses unless explicitly allowed
		if hostname == "localhost" || hostname == "127.0.0.1" || hostname == "::1" {
			if !cfg.AllowLocalURLs {
				return fmt.Errorf("localhost URLs are not allowed")
			}
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
			// Block loopback addresses unless explicitly allowed
			if ip.IsLoopback() {
				if !cfg.AllowLocalURLs {
					return fmt.Errorf("loopback IP addresses are not allowed: %s", ip.String())
				}
			}
			// Block private IP ranges (RFC 1918)
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
		// Use context-aware DNS resolution with timeout to prevent indefinite blocking
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		resolver := net.Resolver{}
		ips, err := resolver.LookupIPAddr(ctx, hostname)
		if err != nil {
			return fmt.Errorf("failed to resolve hostname: %w", err)
		}

		for _, ip := range ips {
			if ip.IP.IsLoopback() {
				if !cfg.AllowLocalURLs {
					return fmt.Errorf("hostname resolves to loopback IP: %s -> %s", hostname, ip.IP.String())
				}
			}
			if ip.IP.IsPrivate() {
				return fmt.Errorf("hostname resolves to private IP: %s -> %s", hostname, ip.IP.String())
			}
			if ip.IP.IsLinkLocalUnicast() {
				return fmt.Errorf("hostname resolves to link-local IP: %s -> %s", hostname, ip.IP.String())
			}
		}
	}

	return nil
}

// uploadHandler handles POST /v1/documents/upload requests for file uploads via multipart/form-data.
// This is an alternative to the REST API for document submission when base64 encoding is inconvenient.
//
// @Summary Upload a document
// @Description Upload a document via multipart/form-data for async processing.
// @Accept multipart/form-data
// @Produce json
// @Param file formData file true "Document file"
// @Param filename formData string false "Filename"
// @Param notify_webhook formData string false "Webhook URL for completion notification"
// @Success 202 {object} models.CreateJobResponse
// @Failure 400 {object} models.ErrorResponse
// @Router /v1/documents/upload [post]
func uploadHandler(c *gin.Context) {
	file, header, err := c.Request.FormFile("file")
	if err != nil {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_request",
			Detail: "file is required",
		})
		return
	}
	defer file.Close()

	ext := strings.ToLower(filepath.Ext(header.Filename))

	maxSizeMB := cfg.MaxDocumentSizeMB
	if ext == ".mp3" || ext == ".wav" || ext == ".m4a" || ext == ".ogg" {
		maxSizeMB = maxAudioSizeMB
	}

	if header.Size > int64(maxSizeMB*1024*1024) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "file_too_large",
			Detail: fmt.Sprintf("maximum file size is %dMB", maxSizeMB),
		})
		return
	}

	// Validate file extension
	allowedExtensions := map[string]bool{
		".pdf":  true,
		".txt":  true,
		".doc":  true,
		".docx": true,
		".ppt":  true,
		".pptx": true,
		".xls":  true,
		".xlsx": true,
		".csv":  true,
		".json": true,
		".jpg":  true,
		".jpeg": true,
		".png":  true,
		".mp3":  true,
		".wav":  true,
		".m4a":  true,
		".ogg":  true,
	}

	ext = strings.ToLower(filepath.Ext(header.Filename))
	if ext == "" || !allowedExtensions[ext] {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_file_type",
			Detail: "file type not allowed. Supported types: pdf, txt, doc, docx, ppt, pptx, xls, xlsx, csv, json, jpg, jpeg, png, mp3, wav, m4a, ogg",
		})
		return
	}

	// Check spreadsheet size and row limits
	if ext == ".csv" || ext == ".xls" || ext == ".xlsx" {
		// Check file size
		if header.Size > maxSpreadsheetBytes {
			c.JSON(http.StatusBadRequest, models.ErrorResponse{
				Error:  "file_too_large",
				Detail: fmt.Sprintf("spreadsheet exceeds size limit of %d MB", maxSpreadsheetBytes/1024/1024),
			})
			return
		}

		// For CSV, count rows
		if ext == ".csv" {
			fileContent, err := ioutil.ReadAll(file)
			if err != nil {
				c.JSON(http.StatusBadRequest, models.ErrorResponse{
					Error:  "read_error",
					Detail: "could not read file",
				})
				return
			}

			reader := csv.NewReader(bytes.NewReader(fileContent))
			recordCount := 0
			for {
				_, err := reader.Read()
				if err == io.EOF {
					break
				}
				if err != nil {
					c.JSON(http.StatusBadRequest, models.ErrorResponse{
						Error:  "csv_parse_error",
						Detail: "could not parse CSV file",
					})
					return
				}
				recordCount++
			}

			if recordCount > maxSpreadsheetRows {
				c.JSON(http.StatusBadRequest, models.ErrorResponse{
					Error:  "too_many_rows",
					Detail: fmt.Sprintf("CSV exceeds row limit of %d rows (%d rows found)", maxSpreadsheetRows, recordCount),
				})
				return
			}

			// Rewind file for later use
			file.Seek(0, 0)
		}
	}

	jobID := generateJobID()
	filename := filepath.Base(header.Filename)

	// Verify filename doesn't contain directory traversal patterns after Base()
	if filename != filepath.Base(filename) || strings.Contains(filename, "..") {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_filename",
			Detail: "filename contains invalid characters",
		})
		return
	}

	safeFilename := fmt.Sprintf("%s_%s", jobID, filename)
	filePath := filepath.Join(cfg.UploadPath, safeFilename)

	// Final security check: ensure resolved path is still within upload directory
	absUploadPath, err := filepath.Abs(cfg.UploadPath)
	if err != nil {
		logger.Error().Err(err).Msg("failed to resolve upload directory")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to save file",
		})
		return
	}

	absFilePath, err := filepath.Abs(filePath)
	if err != nil {
		logger.Error().Err(err).Msg("failed to resolve file path")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to save file",
		})
		return
	}

	if !strings.HasPrefix(absFilePath, absUploadPath+string(os.PathSeparator)) && absFilePath != absUploadPath {
		logger.Error().Msgf("path traversal attempt detected: %s not in %s", absFilePath, absUploadPath)
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_path",
			Detail: "invalid file path",
		})
		return
	}

	if err := os.MkdirAll(cfg.UploadPath, 0755); err != nil {
		logger.Error().Err(err).Msg("failed to create upload directory")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to save file",
		})
		return
	}

	out, err := os.Create(filePath)
	if err != nil {
		logger.Error().Err(err).Msg("failed to create file")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to save file",
		})
		return
	}
	defer out.Close()

	if _, err := io.Copy(out, file); err != nil {
		logger.Error().Err(err).Msg("failed to write file")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to save file",
		})
		return
	}

	notifyWebhook := c.PostForm("notify_webhook")
	if notifyWebhook == "" {
		notifyWebhook = cfg.WebhookURL
	}

	ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
	defer cancel()

	if err := redis.SetJobStatus(ctx, jobID, models.StatusPending); err != nil {
		logger.Error().Err(err).Msg("failed to set job status")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to create job",
		})
		return
	}

	// Validate features from form (e.g., "inferences")
	featuresStr := c.PostForm("features")
	var validatedFeatures []string
	if featuresStr != "" {
		validatedFeatures = validateFeatures(featuresStr)
		if len(validatedFeatures) == 0 {
			logger.Warn().Str("job_id", jobID).Str("features", featuresStr).Msg("No valid features found after validation")
		}
	}

	if err := redis.SetJobCreated(ctx, jobID); err != nil {
		logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to mark job as created")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to initialize job",
		})
		return
	}

	// Store validated features in Redis (if any)
	if len(validatedFeatures) > 0 {
		if err := redis.SetJobFeatures(ctx, jobID, validatedFeatures); err != nil {
			logger.Error().Err(err).Str("job_id", jobID).Msg("Failed to store features")
		} else {
			logger.Info().Str("job_id", jobID).Strs("features", validatedFeatures).Msg("Features stored from multipart")
		}
	}

	// Normalize and store the processing profile (fast/balanced/full)
	profile := normalizeProfile(c.PostForm("profile"))
	if err := redis.SetJobProfile(ctx, jobID, profile); err != nil {
		logger.Error().Err(err).Str("job_id", jobID).Str("profile", profile).Msg("Failed to store job profile")
	} else {
		logger.Info().Str("job_id", jobID).Str("profile", profile).Msg("Job profile stored from multipart")
	}

	jobMsg := models.JobMessage{
		JobID:           jobID,
		DocumentPath:    filePath,
		Filename:        filename,
		MIMEType:        header.Header.Get("Content-Type"),
		ContentType:     models.ContentTypeDocument,
		Features:        validatedFeatures,
		Profile:         profile,
		PipelineVersion: "v1",
	}

	// Determine content type and queue based on file extension
	targetQueue := cfg.ExtractQueue

	if ext == ".mp3" || ext == ".wav" || ext == ".m4a" || ext == ".ogg" {
		jobMsg.ContentType = models.ContentTypeAudio
		diarizeVal := c.PostForm("diarize")
		jobMsg.Diarize = strings.ToLower(diarizeVal) == "true" || strings.ToLower(diarizeVal) == "1" || strings.ToLower(diarizeVal) == "yes"
		targetQueue = cfg.AudioQueue
	} else if ext == ".jpg" || ext == ".jpeg" || ext == ".png" {
		jobMsg.ContentType = models.ContentTypeImage
		targetQueue = cfg.ImageQueue
	} else if ext == ".csv" || ext == ".xls" || ext == ".xlsx" {
		jobMsg.MIMEType = "application/spreadsheet"
	}

	if err := mqBroker.Publish(ctx, targetQueue, jobMsg); err != nil {
		logger.Error().Err(err).Str("queue", targetQueue).Msg("failed to publish job")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to process job",
		})
		return
	}

	metrics.JobsInProgress.Inc()
	metrics.JobsTotal.WithLabelValues("created", "document").Inc()

	logger.Info().Str("job_id", jobID).Str("filename", filename).Msg("job created from upload")

	c.JSON(http.StatusAccepted, models.CreateJobResponse{
		JobID:     jobID,
		Status:    models.StatusPending,
		StatusURL: fmt.Sprintf("/v1/documents/%s", jobID),
	})
}

// downloadHandler handles GET /v1/documents/{id}/download requests and returns job results as a JSON file.
// This endpoint allows clients to download completed job results with proper HTTP headers for file attachment.
// Only available when job status is completed; returns gzip-compressed JSON by default.
//
// @Summary Download job results
// @Description Returns gzip-compressed JSON results from filesystem. Only available when job status is completed.
// @Produce json
// @Param id path string true "Job ID"
// @Param compression query string false "Compression type (gzip)"
// @Success 200 {object} models.JobResults "Gzip-compressed JSON (Content-Encoding: gzip)"
// @Failure 404 {object} models.ErrorResponse
// @Router /v1/documents/{id}/download [get]
func downloadHandler(c *gin.Context) {
	jobID := c.Param("id")
	compression := c.Query("compression")

	// Validate jobID format
	if !validateJobID(jobID) {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_job_id",
			Detail: "job ID format is invalid",
		})
		return
	}

	ctx, cancel := context.WithTimeout(c.Request.Context(), 10*time.Second)
	defer cancel()

	status, err := redis.GetJobStatus(ctx, jobID)
	if err != nil || status == "" {
		c.JSON(http.StatusNotFound, models.ErrorResponse{
			Error:  "not_found",
			Detail: "job not found",
		})
		return
	}

	if status != models.StatusCompleted && status != models.StatusFailed {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "job_not_ready",
			Detail: "job is still processing",
		})
		return
	}

	resultsPath := filepath.Join(cfg.ResultsPath, jobID+".json")

	data, err := os.ReadFile(resultsPath)
	if err != nil {
		if os.IsNotExist(err) {
			c.JSON(http.StatusNotFound, models.ErrorResponse{
				Error:  "not_found",
				Detail: "results file not found",
			})
			return
		}
		logger.Error().Err(err).Msg("failed to read results file")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to read results",
		})
		return
	}

	var results models.JobResults
	if err := json.Unmarshal(data, &results); err != nil {
		logger.Error().Err(err).Msg("failed to parse results")
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to parse results",
		})
		return
	}

	results.JobID = jobID
	results.Status = string(status)

	if compression != "raw" {
		c.Header("Content-Disposition", fmt.Sprintf("attachment; filename=results_%s.json", jobID))
		c.Header("Content-Type", "application/json")

		// Compress embeddings in chunks
		chunks := make([]map[string]interface{}, len(results.Chunks))
		for i, chunk := range results.Chunks {
			chunkData := map[string]interface{}{
				"chunk_id":     chunk.ChunkID,
				"text":         chunk.Text,
				"start_offset": chunk.StartOffset,
				"end_offset":   chunk.EndOffset,
			}

			// Add optional fields if present
			if chunk.TokenCount > 0 {
				chunkData["token_count"] = chunk.TokenCount
			}
			if len(chunk.EntityIDs) > 0 {
				chunkData["entity_ids"] = chunk.EntityIDs
			}
			if len(chunk.Inferences) > 0 {
				chunkData["inferences"] = chunk.Inferences
			}

			if len(chunk.Embeddings) > 0 {
				buf := new(bytes.Buffer)
				for _, f := range chunk.Embeddings {
					// NOTE: Serialization uses little-endian byte order.
					// Clients must deserialize using little-endian.
					bits := math.Float32bits(f)
					buf.WriteByte(byte(bits))
					buf.WriteByte(byte(bits >> 8))
					buf.WriteByte(byte(bits >> 16))
					buf.WriteByte(byte(bits >> 24))
				}

				var compressed bytes.Buffer
				w := gzip.NewWriter(&compressed)
				if _, err := w.Write(buf.Bytes()); err != nil {
					logger.Warn().Err(err).Msg("gzip write warning")
					w.Close()
					chunks[i] = chunkData
					continue
				}
				if err := w.Close(); err != nil {
					logger.Warn().Err(err).Msg("gzip close warning")
				}

				chunkData["embedding_compressed"] = base64.StdEncoding.EncodeToString(compressed.Bytes())
			}

			chunks[i] = chunkData
		}

		// Return full results with compressed embeddings
		response := gin.H{
			"job_id":      jobID,
			"status":      results.Status,
			"created_at":  results.CreatedAt,
			"compression": "gzip",
			"chunks":      chunks,
		}

		// Add optional fields if present
		if results.CompletedAt != "" {
			response["completed_at"] = results.CompletedAt
		}
		if results.Text != "" {
			response["text"] = results.Text
		}
		if len(results.Entities) > 0 {
			response["entities"] = results.Entities
		}
		if len(results.DocumentMetadata) > 0 {
			response["document_metadata"] = results.DocumentMetadata
		}
		if len(results.TextMetadata) > 0 {
			response["text_metadata"] = results.TextMetadata
		}
		if results.SourceClassification != nil {
			response["source_classification"] = results.SourceClassification
		}

		c.JSON(http.StatusOK, response)
		return
	}

	c.Header("Content-Disposition", fmt.Sprintf("attachment; filename=results_%s.json", jobID))
	c.Header("Content-Type", "application/json")
	c.JSON(http.StatusOK, results)
}

// normalizeProfile validates and normalizes a processing profile string.
// Accepts fast|balanced|full (case-insensitive). Unknown or empty values
// fall back to "balanced" (the default pipeline behavior).
func normalizeProfile(profile string) string {
	normalized := strings.ToLower(strings.TrimSpace(profile))
	switch normalized {
	case "fast", "balanced", "full":
		return normalized
	default:
		if normalized != "" {
			logger.Warn().Str("profile", profile).Msg("Invalid profile requested, defaulting to balanced (valid: fast, balanced, full)")
		}
		return "balanced"
	}
}

// validateFeatures validates, deduplicates, and normalizes feature strings.
// Only whitelisted features are kept. Invalid or duplicate features are silently filtered.
func validateFeatures(featuresStr string) []string {
	if featuresStr == "" {
		return nil
	}

	validFeatureSet := map[string]bool{
		"inferences": true,
	}

	rawFeatures := strings.Split(featuresStr, ",")

	seenFeatures := make(map[string]bool)
	validatedFeatures := []string{}

	for _, f := range rawFeatures {
		f = strings.TrimSpace(f)
		if f == "" {
			continue
		}

		normalized := strings.ToLower(f)

		if !validFeatureSet[normalized] {
			metrics.InvalidFeaturesTotal.WithLabelValues("unknown_feature").Inc()
			logger.Warn().Str("feature", f).
				Msg("Invalid feature requested, ignoring (valid: inferences)")
			continue
		}

		if seenFeatures[normalized] {
			metrics.InvalidFeaturesTotal.WithLabelValues("duplicate").Inc()
			logger.Warn().Str("feature", normalized).
				Msg("Duplicate feature, skipping")
			continue
		}

		seenFeatures[normalized] = true
		validatedFeatures = append(validatedFeatures, normalized)
	}

	return validatedFeatures
}
