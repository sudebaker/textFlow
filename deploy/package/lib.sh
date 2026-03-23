#!/usr/bin/env bash
# deploy/package/lib.sh
# Shared logging helpers for all deploy scripts.
# This file must be SOURCED (not executed directly).
# Must be sourced AFTER SCRIPT_NAME is set by the caller.

: "${SCRIPT_NAME:?lib.sh must be sourced after setting SCRIPT_NAME}"

log()  { echo "[${SCRIPT_NAME}] $*"; }
warn() { echo "[${SCRIPT_NAME}] WARNING: $*" >&2; }
die()  { echo "[${SCRIPT_NAME}] ERROR: $*" >&2; exit 1; }
