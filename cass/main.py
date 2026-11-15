"""Camada HTTP do Cass. Por enquanto só o /health."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cass.config import carregar_config


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
    # Falha na subida se faltar configuração, em vez de falhar no primeiro pedido.
    app.state.config = carregar_config()
    yield


app = FastAPI(title="agent-cass", lifespan=ciclo_de_vida)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
