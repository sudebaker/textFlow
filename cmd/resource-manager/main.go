package main

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/rs/zerolog"
	"ia-text-orchestrator/pkg/logging"
)

type ResourceInfo struct {
	GPUAvailable bool     `json:"gpu_available"`
	GPUDevices   []string `json:"gpu_devices"`
	CPUCores     int      `json:"cpu_cores"`
	MemoryBytes  int64    `json:"memory_bytes"`
	GoVersion    string   `json:"go_version"`
	Timestamp    int64    `json:"timestamp"`
}

var logger zerolog.Logger

func main() {
	logging.Init("info")
	logger = logging.GetLogger()

	logger.Info().Msg("Starting Resource Manager")

	r := setupRouter()

	addr := ":9090"
	logger.Info().Msgf("Server starting on %s", addr)

	srv := &http.Server{
		Addr:              addr,
		Handler:           r,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatal().Msgf("Server error: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	logger.Info().Msg("Shutting down server...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Error().Err(err).Msg("failed to shutdown server gracefully")
		srv.Close()
	}
	logger.Info().Msg("Server stopped")
}

func setupRouter() *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())

	r.GET("/health", healthHandler)
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))
	r.GET("/api/v1/resources", getResourceInfo)

	return r
}

func healthHandler(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{
		"status":  "healthy",
		"service": "resource-manager",
	})
}

func getResourceInfo(c *gin.Context) {
	info := ResourceInfo{
		GPUAvailable: detectGPU(),
		GPUDevices:   listGPUDevices(),
		CPUCores:     runtime.NumCPU(),
		MemoryBytes:  getMemory(),
		GoVersion:    runtime.Version(),
		Timestamp:    time.Now().Unix(),
	}

	c.JSON(http.StatusOK, info)
}

func detectGPU() bool {
	gpuAvailable := false

	gpuAvailable = checkNvidiaSMI() || checkCUDA()

	if gpuAvailable {
		logger.Info().Msg("GPU detected")
	} else {
		logger.Info().Msg("No GPU detected, using CPU")
	}

	return gpuAvailable
}

func checkNvidiaSMI() bool {
	return false
}

func checkCUDA() bool {
	return false
}

func listGPUDevices() []string {
	if detectGPU() {
		return []string{"cuda:0"}
	}
	return []string{}
}

func getMemory() int64 {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	return int64(m.Alloc)
}
