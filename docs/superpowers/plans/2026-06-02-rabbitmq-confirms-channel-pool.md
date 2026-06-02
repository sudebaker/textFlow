# RabbitMQ Publisher Confirms & Channel Pool

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add publisher confirms for guaranteed message delivery and implement a channel pool to eliminate single-channel bottleneck in the Go RabbitMQ broker.

**Architecture:** Extend `internal/broker/rabbitmq.go` to support AMQP publisher confirms (ack/nack tracking) and replace the single shared channel with a fixed-size pool of channels multiplexed over one connection. Consumer operations retain a dedicated channel; publishing and management operations use pooled channels.

**Tech Stack:** Go 1.22, `github.com/rabbitmq/amqp091-go`, `sync/atomic`, `context.Context`

---
