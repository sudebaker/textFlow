package config

import (
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/caarlos0/env/v11"
	"github.com/rs/zerolog"
	zerologpkgerrors "github.com/rs/zerolog/pkgerrors"
)

type Config struct {
	RabbitMQURL        string        `env:"RABBITMQ_URL,required"`
	RedisURL           string        `env:"REDIS_URL" default:"redis://localhost:6379"`
	DoclingURL         string        `env:"DOCLING_URL" default:"http://localhost:8000"`
	ResourceManagerURL string        `env:"RESOURCE_MANAGER_URL" default:"http://localhost:9090"`
	HTTPPort           int           `env:"HTTP_PORT" default:"8080"`
	LogLevel           string        `env:"LOG_LEVEL" default:"info"`
	JobTimeout         time.Duration `env:"JOB_TIMEOUT" default:"60m"`
	JobTTL             time.Duration `env:"JOB_TTL" default:"24h"`
	MaxRetries         int           `env:"MAX_RETRIES" default:"3"`
	RetryDelay         time.Duration `env:"RETRY_DELAY" default:"1s"`
	EmbeddingsQueue    string        `env:"EMBEDDINGS_QUEUE" default:"embeddings"`
	EntitiesQueue      string        `env:"ENTITIES_QUEUE" default:"entities"`
	ExtractQueue       string        `env:"EXTRACT_QUEUE" default:"extract_text"`
	MetadataQueue      string        `env:"METADATA_QUEUE" default:"metadata"`
	InferencesQueue    string        `env:"INFERENCES_QUEUE" default:"inferences"`
	AudioQueue         string        `env:"AUDIO_QUEUE" default:"audio"`
	ImageQueue         string        `env:"IMAGE_QUEUE" default:"image"`
	AllowLocalURLs     bool          `env:"ALLOW_LOCAL_URLS" default:"false"`
	WebhookURL         string        `env:"WEBHOOK_URL" default:""`
	UploadPath         string        `env:"UPLOAD_PATH" default:"/app/data/uploads"`
	ResultsPath        string        `env:"RESULTS_PATH" default:"/app/data/results"`
	EntityTypes        []string      `env:"ENTITY_TYPES" envSeparator:","  default:"PERSON,ORGANIZATION,LOCATION"`
	MaxDocumentSizeMB  int           `env:"MAX_DOCUMENT_SIZE_MB" default:"10"`
	MaxFeaturesPerJob  int           `env:"MAX_FEATURES_PER_JOB" default:"10"`
	MaxFeatureNameLen  int           `env:"MAX_FEATURE_NAME_LENGTH" default:"50"`
}

func (c *Config) Validate() error {
	if strings.Contains(c.RabbitMQURL, "guest:guest") {
		return fmt.Errorf("SECURITY: RabbitMQ default credentials detected — set RABBITMQ_URL with proper credentials before deploying")
	}
	return nil
}

func (c *Config) ParseLogLevel() zerolog.Level {
	switch strings.ToLower(c.LogLevel) {
	case "debug":
		return zerolog.DebugLevel
	case "info":
		return zerolog.InfoLevel
	case "warn", "warning":
		return zerolog.WarnLevel
	case "error":
		return zerolog.ErrorLevel
	case "fatal":
		return zerolog.FatalLevel
	case "panic":
		return zerolog.PanicLevel
	default:
		return zerolog.InfoLevel
	}
}

func Load() (*Config, error) {
	cfg := &Config{}
	if err := env.Parse(cfg); err != nil {
		return nil, fmt.Errorf("failed to parse configuration: %w", err)
	}
	if cfg.AudioQueue == "" {
		cfg.AudioQueue = "audio"
	}
	if cfg.ImageQueue == "" {
		cfg.ImageQueue = "image"
	}
	return cfg, nil
}

func InitLogger(level string) zerolog.Logger {
	zerolog.ErrorStackMarshaler = zerologpkgerrors.MarshalStack
	zerolog.TimeFieldFormat = zerolog.TimeFormatUnixMs

	lvl, _ := zerolog.ParseLevel(level)
	if lvl == zerolog.NoLevel {
		lvl = zerolog.InfoLevel
	}

	return zerolog.New(zerolog.ConsoleWriter{Out: os.Stdout, NoColor: false}).
		Level(lvl).
		With().
		Timestamp().
		Logger()
}
