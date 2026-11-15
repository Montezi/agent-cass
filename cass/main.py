"""Camada HTTP do Cass: recebe o pedido e chama o agente.

O formato público é o de docs/api-contract.md (POST /api/agente).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cass.agente import AgenteDemorou, responder
from cass.config import carregar_config
from cass.contrato import para_contrato, resposta_de_erro
from cass.modelos import PedidoAgente, RespostaAgente

logger = logging.getLogger(__name__)


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
    # Falha na subida se faltar configuração, em vez de falhar no primeiro pedido.
    app.state.config = carregar_config()
    yield


app = FastAPI(title="agent-cass", lifespan=ciclo_de_vida)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/api/agente", response_model=RespostaAgente)
async def api_agente(pedido: PedidoAgente, request: Request) -> RespostaAgente | JSONResponse:
    # Pelo contrato, 500 e 504 levam o app ao estado de erro com "perguntar de novo".
    try:
        resposta, ferramentas = await responder(pedido, request.app.state.config)
        return para_contrato(resposta, ferramentas)
    except AgenteDemorou:
        logger.warning("o agente passou do tempo limite")
        return _erro(504, pedido)
    except Exception:
        logger.exception("o agente falhou")
        return _erro(500, pedido)


def _erro(status: int, pedido: PedidoAgente) -> JSONResponse:
    corpo = resposta_de_erro(pedido.language).model_dump()
    return JSONResponse(status_code=status, content=corpo)
