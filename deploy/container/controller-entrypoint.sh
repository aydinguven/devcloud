#!/bin/sh
set -eu

echo "Applying DevCloud database migrations..."
python -m app.migrations upgrade

echo "Starting DevCloud controller..."
exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1 \
    --timeout-graceful-shutdown 10

