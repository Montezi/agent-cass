#!/bin/sh
# Sobe o agent-cass. No Cloud Run, PORT vem do ambiente; fora dele, 8080.
set -eu
decider_ativo="$(printf '%s' "${DECIDER_ATIVO:-false}" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
case "$decider_ativo" in
  true|1|sim|yes)
    exec python -m cass.inicializacao
    ;;
esac
# exec entrega os sinais de encerramento do Cloud Run diretamente ao uvicorn.
exec uvicorn cass.main:app --host 0.0.0.0 --port "${PORT:-8080}" --no-proxy-headers
