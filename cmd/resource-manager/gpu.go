package main

import (
	"context"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// gpuMetrics holds the Prometheus instruments exposed by the poller.
type gpuMetrics struct {
	utilization *prometheus.GaugeVec
	memUsed     *prometheus.GaugeVec
	memTotal    *prometheus.GaugeVec
	temperature *prometheus.GaugeVec
}

func newGPUMetrics() *gpuMetrics {
	labels := []string{"gpu"}
	m := &gpuMetrics{
		utilization: prometheus.NewGaugeVec(
			prometheus.GaugeOpts{
				Name: "resource_manager_gpu_utilization_percent",
				Help: "GPU utilization percentage per device",
			}, labels),
		memUsed: prometheus.NewGaugeVec(
			prometheus.GaugeOpts{
				Name: "resource_manager_gpu_memory_used_bytes",
				Help: "GPU memory used in bytes per device",
			}, labels),
		memTotal: prometheus.NewGaugeVec(
			prometheus.GaugeOpts{
				Name: "resource_manager_gpu_memory_total_bytes",
				Help: "GPU memory total in bytes per device",
			}, labels),
		temperature: prometheus.NewGaugeVec(
			prometheus.GaugeOpts{
				Name: "resource_manager_gpu_temperature_celsius",
				Help: "GPU temperature in celsius per device",
			}, labels),
	}
	prometheus.MustRegister(m.utilization, m.memUsed, m.memTotal, m.temperature)
	return m
}

var (
	smiMu        sync.Mutex
	nvidiaSmiCmd = "nvidia-smi"
)

func nvidiaSMIAvailable() bool {
	smiMu.Lock()
	defer smiMu.Unlock()
	_, err := exec.LookPath(nvidiaSmiCmd)
	return err == nil
}

// queryGPUSamples returns one CSV row per GPU:
// index, name, utilization%, memUsedMiB, memTotalMiB, tempC.
func queryGPUSamples() ([]string, error) {
	smiMu.Lock()
	defer smiMu.Unlock()
	out, err := exec.Command(
		nvidiaSmiCmd,
		"--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
		"--format=csv,noheader,nounits",
	).Output()
	if err != nil {
		return nil, err
	}
	return strings.Split(strings.TrimSpace(string(out)), "\n"), nil
}

func parseGPUSample(row string) (index, name string, utilPct, memUsedMiB, memTotalMiB int64, tempC float64) {
	fields := strings.Split(row, ",")
	if len(fields) < 6 {
		return "", "", 0, 0, 0, 0
	}
	trim := func(i int) string { return strings.TrimSpace(fields[i]) }
	index = trim(0)
	name = trim(1)
	utilPct, _ = strconv.ParseInt(trim(2), 10, 64)
	memUsedMiB, _ = strconv.ParseInt(trim(3), 10, 64)
	memTotalMiB, _ = strconv.ParseInt(trim(4), 10, 64)
	tempC, _ = strconv.ParseFloat(trim(5), 64)
	return
}

func (m *gpuMetrics) update(rows []string) {
	for _, row := range rows {
		idx, _, util, usedMiB, totalMiB, temp := parseGPUSample(row)
		if idx == "" {
			continue
		}
		m.utilization.WithLabelValues(idx).Set(float64(util))
		m.memUsed.WithLabelValues(idx).Set(float64(usedMiB) * 1024 * 1024)
		m.memTotal.WithLabelValues(idx).Set(float64(totalMiB) * 1024 * 1024)
		m.temperature.WithLabelValues(idx).Set(temp)
	}
}

// startGPUPoller samples nvidia-smi periodically until ctx is cancelled.
// It is a no-op when nvidia-smi is not available.
func startGPUPoller(ctx context.Context, interval time.Duration) {
	if !nvidiaSMIAvailable() {
		logger.Info().Msg("nvidia-smi not available; GPU metrics disabled")
		return
	}
	m := newGPUMetrics()
	logger.Info().Dur("interval", interval).Msg("GPU metrics poller started")

	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			rows, err := queryGPUSamples()
			if err != nil {
				logger.Warn().Err(err).Msg("nvidia-smi query failed")
			} else {
				m.update(rows)
			}
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
}
