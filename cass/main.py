"""Camada HTTP do Cass: recebe o pedido e chama o agente."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from cass.agente import criar_agente
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


# Provisório: o passo 4 troca estes dois por Pedido e RespostaCass (cass/modelos.py).
class PerguntaProvisoria(BaseModel):
    pergunta: str = Field(min_length=1, max_length=1000)


class RespostaProvisoria(BaseModel):
    texto: str


@app.post("/chat")
async def chat(pedido: PerguntaProvisoria, request: Request) -> RespostaProvisoria:
    agente = criar_agente(request.app.state.config)
    resultado = await agente.invoke_async(pedido.pergunta)
    return RespostaProvisoria(texto=str(resultado).strip())
