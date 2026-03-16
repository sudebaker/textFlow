package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
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
	cfg           *config.Config
	mqBroker      *broker.RabbitMQBroker
	redis         *redisclient.RedisClient
	eventBus      *events.EventBus
	healthChecker *health.HealthChecker
	logger        zerolog.Logger
)

func main() {
	var err error

	logging.Init("info")
	logger = logging.GetLogger()

	cfg, err = config.Load()
	if err != nil {
		logger.Fatal().Msgf("Failed to load configuration: %v", err)
	}

	// Re-initialize logger with configured log level
	logger = logging.Init(cfg.LogLevel)

	logger.Info().Msg("Starting IA Text Orchestrator")

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

	// Initialize comprehensive health checker
	healthChecker = health.NewHealthChecker(redis, mqBroker, cfg)
	logger.Info().Msg("Health checker initialized")

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
		v1.POST("/documents/upload", uploadHandler)
		v1.GET("/documents/:id", getJobHandler)
		v1.GET("/documents/:id/download", downloadHandler)
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
		EntityTypes:    req.EntityTypes,
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

	// Get aggregated results (if completed)
	var results *models.JobResults
	var resultsErr error
	if status == models.StatusCompleted {
		results, resultsErr = redis.GetJobResults(ctx, jobID)
		if resultsErr != nil {
			logger.Error().Err(resultsErr).Msgf("Failed to get job results: %s", jobID)
		} else if results == nil {
			logger.Warn().Msgf("GetJobResults returned nil for job: %s", jobID)
		} else {
			logger.Info().Msgf("Got results: job_id=%s, chunks=%d, embeddings=%d",
				results.JobID, len(results.Chunks), len(results.Embeddings))
		}
	}

	errorMsg, _ := redis.GetJobError(ctx, jobID)

	// Get created timestamp
	createdAt, _ := redis.GetJobCreated(ctx, jobID)

	c.JSON(http.StatusOK, models.GetJobResponse{
		JobID:     jobID,
		Status:    status,
		Results:   results,
		Error:     errorMsg,
		CreatedAt: createdAt,
	})
}

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
	return uuid.New().String()
}

// validateJobID validates that a jobID parameter matches expected format (UUID v4)
// Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (alphanumeric + hyphens, 36 chars)
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

// validateDocumentInput validates the document input to prevent DoS and SSRF attacks
func validateDocumentInput(req *models.CreateJobRequest, cfg *config.Config) error {
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

	if header.Size > 10*1024*1024 {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "file_too_large",
			Detail: "maximum file size is 10MB",
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
	}

	ext := strings.ToLower(filepath.Ext(header.Filename))
	if ext == "" || !allowedExtensions[ext] {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_file_type",
			Detail: "file type not allowed. Supported types: pdf, txt, doc, docx, ppt, pptx, xls, xlsx, csv, json",
		})
		return
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

	entityTypes := c.PostForm("entity_types")
	var entityTypesList []string
	if entityTypes != "" {
		entityTypesList = strings.Split(entityTypes, ",")
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

	if err := redis.SetJobCreated(ctx, jobID); err != nil {
		logger.Error().Err(err).Msg("failed to set job created time")
	}

	jobMsg := models.JobMessage{
		JobID:         jobID,
		DocumentPath:  filePath,
		MIMEType:      header.Header.Get("Content-Type"),
		EntityTypes:   entityTypesList,
		NotifyWebhook: notifyWebhook,
	}

	if err := mqBroker.Publish(ctx, cfg.ExtractQueue, jobMsg); err != nil {
		logger.Error().Err(err).Msg("failed to publish job")
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

func downloadHandler(c *gin.Context) {
	jobID := c.Param("id")

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

	c.Header("Content-Disposition", fmt.Sprintf("attachment; filename=results_%s.json", jobID))
	c.Header("Content-Type", "application/json")
	c.JSON(http.StatusOK, results)
}
