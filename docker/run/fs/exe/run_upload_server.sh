#!/bin/bash

. "/ins/setup_venv.sh" "$@"

export AGENTZERO_UPLOAD_DIR="${AGENTZERO_UPLOAD_DIR:-/app/work_dir/assets/agentzero_uploads/shorts_test}"
export AGENTZERO_UPLOAD_HOST="${AGENTZERO_UPLOAD_HOST:-0.0.0.0}"
export AGENTZERO_UPLOAD_PORT="${AGENTZERO_UPLOAD_PORT:-8080}"

exec python /git/agent-zero/tools/phone_upload_server.py
