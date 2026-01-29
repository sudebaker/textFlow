package logging

import (
	"os"
	"sync"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/pkgerrors"
)

var (
	logger     zerolog.Logger
	loggerInit sync.Once
)

func Init(level string) zerolog.Logger {
	loggerInit.Do(func() {
		zerolog.ErrorStackMarshaler = pkgerrors.MarshalStack
		zerolog.TimeFieldFormat = zerolog.TimeFormatUnixMs

		writer := zerolog.ConsoleWriter{
			Out:           os.Stdout,
			TimeFormat:    "2006-01-02 15:04:05.000",
			FormatLevel:   func(i interface{}) string { return "[" + i.(string) + "]" },
			FormatMessage: func(i interface{}) string { return i.(string) },
			FormatCaller:  func(i interface{}) string { return i.(string) },
		}

		lvl, _ := zerolog.ParseLevel(level)
		if lvl == zerolog.NoLevel {
			lvl = zerolog.InfoLevel
		}

		logger = zerolog.New(writer).
			Level(lvl).
			With().
			Timestamp().
			Logger()
	})
	return logger
}

func GetLogger() zerolog.Logger {
	return logger
}

func With() zerolog.Context {
	return logger.With()
}

func Debug() *zerolog.Event {
	return logger.Debug()
}

func Info() *zerolog.Event {
	return logger.Info()
}

func Warn() *zerolog.Event {
	return logger.Warn()
}

func Error() *zerolog.Event {
	return logger.Error()
}

func Fatal() *zerolog.Event {
	return logger.Fatal()
}

func Panic() *zerolog.Event {
	return logger.Panic()
}
