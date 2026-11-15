"""Camada HTTP do Cass: recebe o pedido e chama o agente.

O formato público é o de docs/api-contract.md (POST /api/agente).
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress

from anyio import CancelScope
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Send

from cass.agente import AgenteDemorou, responder
from cass.config import Configuracao, carregar_config
from cass.contrato import para_contrato, resposta_de_erro
from cass.hooks import EventoProgresso, ProgressoERegistro
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


class _RespostaSSE(StreamingResponse):
    """Fecha o gerador também se o cliente desconectar durante o envio de um evento."""

    def __init__(self, eventos: AsyncGenerator[str, None]) -> None:
        super().__init__(
            eventos,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self._eventos = eventos

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # A Starlette cancela o consumidor ao receber http.disconnect. O shield
            # permite que o finally do gerador cancele e aguarde o agente.
            with CancelScope(shield=True):
                await self._eventos.aclose()


@app.post(
    "/api/agente/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def api_agente_stream(pedido: PedidoAgente, request: Request) -> StreamingResponse:
    return _RespostaSSE(_eventos_agente(pedido, request.app.state.config))


async def _eventos_agente(
    pedido: PedidoAgente, config: Configuracao
) -> AsyncGenerator[str, None]:
    """Envia o progresso em ordem e uma única resposta ou erro terminal."""
    fila: asyncio.Queue[EventoProgresso | None] = asyncio.Queue()
    progresso = ProgressoERegistro(fila)

    async def produzir() -> None:
        try:
            resposta, ferramentas = await responder(pedido, config, progresso=progresso)
            corpo = para_contrato(resposta, ferramentas).model_dump()
            fila.put_nowait(("resposta", corpo))
        except AgenteDemorou:
            logger.warning("o agente passou do tempo limite no stream")
            fila.put_nowait(
                ("erro", {"status": 504, "response": resposta_de_erro(pedido.language).model_dump()})
            )
        except Exception:
            logger.exception("o agente falhou no stream")
            fila.put_nowait(
                ("erro", {"status": 500, "response": resposta_de_erro(pedido.language).model_dump()})
            )
        finally:
            fila.put_nowait(None)

    tarefa = asyncio.create_task(produzir())
    try:
        while (evento := await fila.get()) is not None:
            nome, dados = evento
            yield f"event: {nome}\ndata: {json.dumps(dados, ensure_ascii=False)}\n\n"
    finally:
        tarefa.cancel()
        with CancelScope(shield=True), suppress(asyncio.CancelledError):
            await tarefa
