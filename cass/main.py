"""Camada HTTP do Cass: recebe o pedido e chama o agente.

O formato público é o de docs/api-contract.md (POST /api/agente e GET /api/clima).
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated

from anyio import CancelScope
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Send

from cass.agente import TEMPO_LIMITE_S, AgenteDemorou, responder
from cass.config import Configuracao, carregar_config
from cass.contrato import para_contrato, resposta_de_erro
from cass.ferramentas import PrevisaoIndisponivel, hotspots_por_id, ids_dos_hotspots
from cass.ferramentas import aquecer_previsoes, previsao_do_hotspot
from cass.guardrails import avaliar_entrada
from cass.hooks import EventoProgresso, ProgressoERegistro
from cass.limites import CORSConfigurado, LimitadorPorIP, LimiteDeCorpo, verificar_limite_ip
from cass.modelos import PADRAO_ID, PedidoAgente, PrevisaoClima, RespostaAgente, RespostaCass
from cass.verificacao import validar_resposta

logger = logging.getLogger(__name__)


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
    # Falha na subida se faltar configuração, em vez de falhar no primeiro pedido.
    app.state.config = carregar_config()
    app.state.limite_ip = LimitadorPorIP()
    app.state.limite_ip_config = app.state.config
    # Em segundo plano: o /health responde enquanto as previsões dos hotspots chegam.
    aquecimento = asyncio.create_task(_aquecer_previsoes())
    yield
    aquecimento.cancel()
    with suppress(asyncio.CancelledError):
        await aquecimento


async def _aquecer_previsoes() -> None:
    """Enche o cache da previsão de cada hotspot; uma falha só vai para o log."""
    try:
        fontes = await asyncio.to_thread(aquecer_previsoes)
    except Exception as erro:
        logger.warning("aquecimento das previsões falhou: %s", type(erro).__name__)
        return
    logger.info("previsões aquecidas: %s", fontes)


app = FastAPI(title="agent-cass", lifespan=ciclo_de_vida)
# O último registrado é o mais externo: o CORS envolve o limite de corpo, e a recusa
# 413 também sai com os cabeçalhos de origem.
app.add_middleware(LimiteDeCorpo)
app.add_middleware(CORSConfigurado)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


# O app manda lat e lon do próprio hotspots.geojson (String() do número do JSON), então
# coincidem; a folga só cobre arredondamento.
TOLERANCIA_GRAUS = 1e-6


@app.get("/api/clima", response_model=PrevisaoClima)
def api_clima(
    hotspot: Annotated[str, Query(pattern=PADRAO_ID, max_length=64)],
    lat: Annotated[float, Query(ge=-90, le=90, allow_inf_nan=False)],
    lon: Annotated[float, Query(ge=-180, le=180, allow_inf_nan=False)],
) -> PrevisaoClima | JSONResponse:
    """Chuva por dia no hotspot (api-contract.md): Meteomatics, Open-Meteo ou snapshot.

    Só hotspots do hotspots.geojson, com as coordenadas de lá: o serviço não consulta
    as fontes para um ponto qualquer. Síncrona: o FastAPI a roda numa thread.
    """
    hotspots = hotspots_por_id()
    if hotspots is None:
        logger.warning("hotspots.geojson indisponível na /api/clima")
        return JSONResponse(status_code=503, content={"detail": "lista de hotspots indisponível"})
    alvo = hotspots.get(hotspot)
    if alvo is None:
        return _erro_de_consulta(
            "hotspot_desconhecido", "hotspot", "hotspot fora do hotspots.geojson",
            {"validos": list(hotspots)},
        )
    lon_do_hotspot, lat_do_hotspot = alvo.geometry.coordinates
    if abs(lat - lat_do_hotspot) > TOLERANCIA_GRAUS or abs(lon - lon_do_hotspot) > TOLERANCIA_GRAUS:
        return _erro_de_consulta(
            "coordenadas_do_hotspot", "lat", "lat e lon precisam ser as do hotspot no hotspots.geojson"
        )
    try:
        return previsao_do_hotspot(alvo).previsao
    except PrevisaoIndisponivel:
        # Os motivos ficam no log (cass.ferramentas); o app lê o snapshot por conta própria.
        return JSONResponse(status_code=503, content={"detail": "nenhuma fonte de previsão respondeu"})


def _erro_de_consulta(tipo: str, campo: str, msg: str, ctx: dict | None = None) -> JSONResponse:
    """422 no formato de erro de validação do FastAPI, sem refletir o valor recebido."""
    erro = {"type": tipo, "loc": ["query", campo], "msg": msg, **({"ctx": ctx} if ctx else {})}
    return JSONResponse(status_code=422, content={"detail": [erro]})


@app.post("/api/agente", response_model=RespostaAgente)
async def api_agente(pedido: PedidoAgente, request: Request) -> RespostaAgente | JSONResponse:
    if (recusa := _limitar_ip(pedido, request)) is not None:
        return recusa
    pedido, recusa = await _conferir_hotspot(pedido)
    if recusa is not None:
        return recusa
    # Pelo contrato, 500 e 504 levam o app ao estado de erro com "perguntar de novo".
    try:
        resposta, ferramentas = await _responder_protegido(pedido, request.app.state.config)
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


async def _conferir_hotspot(pedido: PedidoAgente) -> tuple[PedidoAgente, JSONResponse | None]:
    """hotspot_id precisa ser um id do hotspots.geojson (api-contract.md do frontend).

    Id desconhecido: 422, no formato de erro de validação do FastAPI. Se a lista
    não puder ser lida, o pedido segue sem hotspot_id: um id sem conferência não
    chega ao steering nem ao prompt.
    """
    if pedido.hotspot_id is None:
        return pedido, None
    ids = await asyncio.to_thread(ids_dos_hotspots)
    if ids is None:
        logger.warning("hotspots.geojson indisponível; o pedido segue sem hotspot_id")
        return pedido.model_copy(update={"hotspot_id": None}), None
    if pedido.hotspot_id in ids:
        return pedido, None
    erro = {
        "type": "hotspot_desconhecido",
        "loc": ["body", "hotspot_id"],
        "msg": f"hotspot desconhecido: {pedido.hotspot_id!r}",
        "input": pedido.hotspot_id,
        "ctx": {"validos": ids},
    }
    return pedido, JSONResponse(status_code=422, content={"detail": [erro]})


def _limitar_ip(pedido: PedidoAgente, request: Request) -> JSONResponse | None:
    espera = verificar_limite_ip(request)
    if espera is None:
        return None
    texto = (
        "Muitas perguntas em pouco tempo. Aguarde um pouco e tente de novo."
        if pedido.language == "pt"
        else "Too many questions in a short time. Please wait and try again."
    )
    corpo = RespostaAgente(paragraphs=[texto], tools=[], sources=[]).model_dump()
    return JSONResponse(status_code=429, content=corpo, headers={"Retry-After": str(espera)})


async def _responder_protegido(
    pedido: PedidoAgente,
    config: Configuracao,
    progresso: ProgressoERegistro | None = None,
) -> tuple[RespostaCass, list[str]]:
    """Camadas de entrada e saída compartilhadas pelo JSON e pelo stream."""
    progresso = progresso if progresso is not None else ProgressoERegistro()
    progresso.iniciar()
    tipo = "erro"
    try:
        async with asyncio.timeout(TEMPO_LIMITE_S):
            recusa = await asyncio.to_thread(avaliar_entrada, pedido)
            if recusa is not None:
                tipo = recusa.tipo
                return recusa, []
            resposta, ferramentas = await responder(
                pedido, config, progresso=progresso, validar=validar_resposta
            )
            tipo = resposta.tipo
            return resposta, ferramentas
    except TimeoutError as e:
        raise AgenteDemorou(f"passou de {TEMPO_LIMITE_S} s") from e
    finally:
        progresso.finalizar(tipo)


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
    response_model=None,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def api_agente_stream(
    pedido: PedidoAgente, request: Request
) -> StreamingResponse | JSONResponse:
    if (recusa := _limitar_ip(pedido, request)) is not None:
        return recusa
    # Antes de abrir o stream, para a recusa sair com o status certo.
    pedido, recusa = await _conferir_hotspot(pedido)
    if recusa is not None:
        return recusa
    return _RespostaSSE(_eventos_agente(pedido, request.app.state.config))


async def _eventos_agente(
    pedido: PedidoAgente, config: Configuracao
) -> AsyncGenerator[str, None]:
    """Envia o progresso em ordem e uma única resposta ou erro terminal."""
    fila: asyncio.Queue[EventoProgresso | None] = asyncio.Queue()
    progresso = ProgressoERegistro(fila)

    async def produzir() -> None:
        try:
            resposta, ferramentas = await _responder_protegido(pedido, config, progresso=progresso)
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
