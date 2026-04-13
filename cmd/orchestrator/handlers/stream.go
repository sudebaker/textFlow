package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"textflow/internal/broker"
	"textflow/internal/events"
	"textflow/internal/models"
	redisclient "textflow/internal/redis"
	"textflow/pkg/logging"
)

const (
	sseChannelBuffer = 100
	sseHeartbeat     = 30 * time.Second
	sseMaxDuration   = 10 * time.Minute
)

var (
	eventBus  *events.EventBus
	redisInst *redisclient.RedisClient
	mqBroker  *broker.RabbitMQBroker
)

func SetDependencies(eb *events.EventBus, r *redisclient.RedisClient, mq *broker.RabbitMQBroker) {
	eventBus = eb
	redisInst = r
	mqBroker = mq
}

func StreamJobHandler(c *gin.Context) {
	jobID := c.Param("id")

	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()

	status, err := redisInst.GetJobStatus(ctx, jobID)
	if err != nil || status == "" {
		c.JSON(http.StatusNotFound, models.ErrorResponse{
			Error:  "not_found",
			Detail: "job not found",
		})
		return
	}

	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")

	if status == models.StatusCompleted || status == models.StatusFailed {
		eventType := "job_completed"
		if status == models.StatusFailed {
			eventType = "job_failed"
		}
		eventData, err := json.Marshal(map[string]interface{}{
			"job_id":    jobID,
			"status":    string(status),
			"timestamp": time.Now().Format(time.RFC3339),
		})
		if err != nil {
			logging.Warn().Err(err).Msg("failed to marshal event")
			return
		}
		c.SSEvent(eventType, string(eventData))
		c.Writer.Flush()
		return
	}

	pubsub := eventBus.Subscribe(c.Request.Context(), fmt.Sprintf("job:%s:events", jobID))
	defer pubsub.Close()

	done := make(chan struct{})

	go func() {
		tick := time.NewTicker(sseHeartbeat)
		defer tick.Stop()
		for {
			select {
			case <-tick.C:
				c.Writer.WriteString(": heartbeat\n\n")
				c.Writer.Flush()
			case <-done:
				return
			}
		}
	}()

	ctx, cancel = context.WithTimeout(ctx, sseMaxDuration)
	defer cancel()
	defer close(done)

	ch := pubsub.Channel()
	for {
		select {
		case msg := <-ch:
			var jobEvent events.JobEvent
			if err := json.Unmarshal([]byte(msg.Payload), &jobEvent); err != nil {
				continue
			}

			eventType := string(jobEvent.EventType)
			eventData, err := json.Marshal(map[string]interface{}{
				"job_id":    jobEvent.JobID,
				"status":    jobEvent.Status,
				"progress":  jobEvent.Progress,
				"timestamp": jobEvent.Timestamp.Format(time.RFC3339),
				"error":     jobEvent.Error,
			})
			if err != nil {
				logging.Warn().Err(err).Msg("failed to marshal event")
				continue
			}

			c.SSEvent(eventType, string(eventData))
			c.Writer.Flush()

			if jobEvent.EventType == events.EventJobCompleted || jobEvent.EventType == events.EventJobFailed {
				return
			}

		case <-ctx.Done():
			return
		}
	}
}
