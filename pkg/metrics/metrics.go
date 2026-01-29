package metrics

import (
	"runtime"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	// HTTP metrics
	HTTPRequestsTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ia_text_http_requests_total",
			Help: "Total number of HTTP requests",
		},
		[]string{"method", "endpoint", "status"},
	)

	HTTPLatencySeconds = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "ia_text_http_latency_seconds",
			Help:    "HTTP request latency in seconds",
			Buckets: prometheus.DefBuckets,
		},
		[]string{"method", "endpoint"},
	)

	// Job metrics
	JobsTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ia_text_jobs_total",
			Help: "Total number of jobs processed",
		},
		[]string{"status", "type"},
	)

	JobDurationSeconds = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "ia_text_job_duration_seconds",
			Help:    "Job processing duration in seconds",
			Buckets: []float64{5, 10, 30, 60, 120, 300, 600},
		},
		[]string{"type"},
	)

	JobsInProgress = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "ia_text_jobs_in_progress",
			Help: "Number of jobs currently being processed",
		},
	)

	// Queue metrics
	QueueDepth = promauto.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ia_text_queue_depth",
			Help: "Number of messages in each queue",
		},
		[]string{"queue"},
	)

	QueuePublishTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ia_text_queue_publish_total",
			Help: "Total messages published to each queue",
		},
		[]string{"queue"},
	)

	QueueConsumeTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ia_text_queue_consume_total",
			Help: "Total messages consumed from each queue",
		},
		[]string{"queue"},
	)

	// Redis metrics
	RedisLatencySeconds = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "ia_text_redis_latency_seconds",
			Help:    "Redis operation latency in seconds",
			Buckets: []float64{0.001, 0.005, 0.01, 0.05, 0.1, 0.5},
		},
		[]string{"operation"},
	)

	RedisErrors = promauto.NewCounter(
		prometheus.CounterOpts{
			Name: "ia_text_redis_errors_total",
			Help: "Total Redis errors",
		},
	)

	// RabbitMQ metrics
	RabbitMQErrors = promauto.NewCounter(
		prometheus.CounterOpts{
			Name: "ia_text_rabbitmq_errors_total",
			Help: "Total RabbitMQ errors",
		},
	)

	// Runtime metrics
	GoroutineCount = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "ia_text_goroutine_count",
			Help: "Number of goroutines currently running",
		},
	)

	MemoryAllocBytes = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "ia_text_memory_alloc_bytes",
			Help: "Number of bytes allocated and still in use",
		},
	)

	MemorySysBytes = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "ia_text_memory_sys_bytes",
			Help: "Number of bytes obtained from system",
		},
	)

	// Job step metrics
	JobStepDuration = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "ia_text_job_step_duration_seconds",
			Help:    "Duration of individual job steps in seconds",
			Buckets: []float64{1, 5, 10, 30, 60, 120, 300},
		},
		[]string{"step", "job_type"},
	)

	// Queue consumer lag
	QueueConsumerLag = promauto.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ia_text_queue_consumer_lag",
			Help: "Consumer lag per queue (messages waiting)",
		},
		[]string{"queue"},
	)

	// Cache metrics
	CacheHits = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ia_text_cache_hits_total",
			Help: "Total number of cache hits",
		},
		[]string{"cache_type"},
	)

	CacheMisses = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ia_text_cache_misses_total",
			Help: "Total number of cache misses",
		},
		[]string{"cache_type"},
	)
)

// Init initializes queue metrics with default values
func Init() {
	QueueDepth.WithLabelValues("embeddings").Set(0)
	QueueDepth.WithLabelValues("entities").Set(0)
	QueueDepth.WithLabelValues("metadata").Set(0)

	QueueConsumerLag.WithLabelValues("embeddings").Set(0)
	QueueConsumerLag.WithLabelValues("entities").Set(0)
	QueueConsumerLag.WithLabelValues("metadata").Set(0)
}

// StartMetricsCollector starts a goroutine that periodically collects runtime metrics
func StartMetricsCollector() {
	go func() {
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()

		for range ticker.C {
			collectRuntimeMetrics()
		}
	}()
}

// collectRuntimeMetrics gathers Go runtime metrics
func collectRuntimeMetrics() {
	var memStats runtime.MemStats
	runtime.ReadMemStats(&memStats)

	GoroutineCount.Set(float64(runtime.NumGoroutine()))
	MemoryAllocBytes.Set(float64(memStats.Alloc))
	MemorySysBytes.Set(float64(memStats.Sys))
}
