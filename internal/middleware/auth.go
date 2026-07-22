package middleware

import (
	"crypto/subtle"
	"net/http"

	"github.com/gin-gonic/gin"
)

func APIKeyAuth(expectedKey string) gin.HandlerFunc {
	return func(c *gin.Context) {
		if expectedKey == "" {
			c.Next()
			return
		}
		provided := c.GetHeader("X-API-Key")
		if provided == "" || subtle.ConstantTimeCompare([]byte(provided), []byte(expectedKey)) != 1 {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid_api_key"})
			return
		}
		c.Next()
	}
}
