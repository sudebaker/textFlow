package handlers

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"ia-text-orchestrator/internal/models"
	redisclient "ia-text-orchestrator/internal/redis"
)

func CreateBatchHandler(c *gin.Context) {
	var req BatchRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_request",
			Detail: err.Error(),
		})
		return
	}

	if len(req.Documents) > 100 {
		c.JSON(http.StatusBadRequest, models.ErrorResponse{
			Error:  "invalid_request",
			Detail: "maximum 100 documents per batch",
		})
		return
	}

	maxConcurrency := req.MaxConcurrency
	if maxConcurrency <= 0 {
		maxConcurrency = 10
	}
	if maxConcurrency > 50 {
		maxConcurrency = 50
	}

	batchID := uuid.New().String()
	now := time.Now()

	batchMetaKey := redisclient.Key("batch", batchID, "meta")
	ctx := c.Request.Context()

	err := redisclient.GetClient().HSet(ctx, batchMetaKey, map[string]interface{}{
		"total":          len(req.Documents),
		"created_at":     now.Unix(),
		"webhook_url":    req.WebhookURL,
		"webhook_secret": req.WebhookSecret,
	}).Err()
	if err != nil {
		c.JSON(http.StatusInternalServerError, models.ErrorResponse{
			Error:  "internal_error",
			Detail: "failed to create batch",
		})
		return
	}
	redisclient.GetClient().Expire(ctx, batchMetaKey, 24*time.Hour)

	batchJobsKey := redisclient.Key("batch", batchID, "jobs")
	redisclient.GetClient().SAdd(ctx, batchJobsKey, "")

	semaphore := make(chan struct{}, maxConcurrency)
	var wg sync.WaitGroup
	jobs := make([]BatchJobRef, len(req.Documents))

	for i, doc := range req.Documents {
		wg.Add(1)
		go func(i int, doc BatchDocument) {
			defer wg.Done()
			semaphore <- struct{}{}
			defer func() { <-semaphore }()

			jobID := uuid.New().String()

			jobCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
			defer cancel()

			redisInst.SetJobStatus(jobCtx, jobID, models.StatusPending)
			redisInst.SetJobCreated(jobCtx, jobID)

			redisclient.GetClient().HSet(jobCtx, redisclient.Key("job", jobID, "meta"), "batch_id", batchID)
			redisclient.GetClient().SAdd(jobCtx, batchJobsKey, jobID)
			redisclient.GetClient().Expire(jobCtx, batchJobsKey, 24*time.Hour)

			if req.WebhookURL != "" {
				redisInst.SetJobWebhook(jobCtx, jobID, req.WebhookURL, req.WebhookSecret)
			}

			jobMsg := &models.JobMessage{
				JobID:    jobID,
				Filename: doc.Filename,
			}

			mqBroker.PublishJobMessage(jobCtx, jobMsg)

			jobs[i] = BatchJobRef{
				ID:       jobID,
				Filename: doc.Filename,
				Status:   "pending",
			}
		}(i, doc)
	}

	wg.Wait()

	c.JSON(http.StatusAccepted, BatchResponse{
		BatchID:   batchID,
		Total:     len(req.Documents),
		Jobs:      jobs,
		StatusURL: fmt.Sprintf("/v1/batches/%s/status", batchID),
		CreatedAt: now,
	})
}

func GetBatchStatusHandler(c *gin.Context) {
	batchID := c.Param("id")
	ctx := c.Request.Context()

	meta, err := redisclient.GetClient().HGetAll(ctx, redisclient.Key("batch", batchID, "meta")).Result()
	if err != nil || len(meta) == 0 {
		c.JSON(http.StatusNotFound, models.ErrorResponse{
			Error:  "not_found",
			Detail: "batch not found",
		})
		return
	}

	jobs, _ := redisclient.GetClient().SMembers(ctx, redisclient.Key("batch", batchID, "jobs")).Result()

	var completed, failed, pending int
	jobRefs := make([]BatchJobRef, 0, len(jobs))

	for _, jobID := range jobs {
		if jobID == "" {
			continue
		}
		status, _ := redisInst.GetJobStatus(ctx, jobID)
		switch status {
		case models.StatusCompleted:
			completed++
		case models.StatusFailed:
			failed++
		default:
			pending++
		}
		jobRefs = append(jobRefs, BatchJobRef{
			ID:     jobID,
			Status: string(status),
		})
	}

	total := len(jobs)
	var batchStatus string
	if pending == 0 {
		if failed == total {
			batchStatus = "failed"
		} else if failed > 0 {
			batchStatus = "partial"
		} else {
			batchStatus = "completed"
		}
	} else {
		batchStatus = "running"
	}

	createdAt := time.Unix(0, 0)
	if ts, ok := meta["created_at"]; ok {
		if t, err := strconv.ParseInt(ts, 10, 64); err == nil {
			createdAt = time.Unix(t, 0)
		}
	}

	c.JSON(http.StatusOK, BatchStatusResponse{
		BatchID:   batchID,
		Status:    batchStatus,
		Total:     total,
		Completed: completed,
		Failed:    failed,
		Pending:   pending,
		Jobs:      jobRefs,
		CreatedAt: createdAt,
	})
}
