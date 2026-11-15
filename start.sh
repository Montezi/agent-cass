#!/bin/sh
# Sobe o agent-cass. No Cloud Run, PORT vem do ambiente; fora dele, 8080.
set -eu
exec uvicorn cass.main:app --host 0.0.0.0 --port "${PORT:-8080}"
