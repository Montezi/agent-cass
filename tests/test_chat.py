"""Testes do POST /chat provisório, com um agente falso (sem chamar o Gemini)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cass import main
from cass.config import Configuracao


class AgenteFalso:
    def __init__(self) -> None:
        self.perguntas: list[str] = []

    async def invoke_async(self, pergunta: str) -> str:
        self.perguntas.append(pergunta)
        return "Os dados do Guaíba são de exemplo.\n"


@pytest.fixture
def agente(monkeypatch: pytest.MonkeyPatch) -> AgenteFalso:
    falso = AgenteFalso()
    configs: list[Configuracao] = []

    def criar_agente_falso(config: Configuracao) -> AgenteFalso:
        configs.append(config)
        return falso

    monkeypatch.setattr(main, "criar_agente", criar_agente_falso)
    falso.configs = configs  # type: ignore[attr-defined]
    return falso


@pytest.fixture
def cliente(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    with TestClient(main.app) as c:
        yield c


def test_chat_devolve_o_texto_do_agente(cliente: TestClient, agente: AgenteFalso) -> None:
    resposta = cliente.post("/chat", json={"pergunta": "O que mudou no Guaíba?"})

    assert resposta.status_code == 200
    assert resposta.json() == {"texto": "Os dados do Guaíba são de exemplo."}
    assert agente.perguntas == ["O que mudou no Guaíba?"]


def test_chat_usa_a_config_da_subida(cliente: TestClient, agente: AgenteFalso) -> None:
    cliente.post("/chat", json={"pergunta": "oi"})

    assert agente.configs[0].google_cloud_project == "projeto-teste"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "corpo",
    [
        pytest.param({}, id="sem-pergunta"),
        pytest.param({"pergunta": ""}, id="vazia"),
        pytest.param({"pergunta": "x" * 1001}, id="mais-de-1000"),
        pytest.param({"pergunta": 42}, id="nao-e-texto"),
    ],
)
def test_chat_recusa_pedido_invalido(
    cliente: TestClient, agente: AgenteFalso, corpo: dict
) -> None:
    resposta = cliente.post("/chat", json=corpo)

    assert resposta.status_code == 422
    assert agente.perguntas == []
