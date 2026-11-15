"""SSE com o agente simulado, incluindo entrega incremental e desconexão ASGI."""

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from cass import main
from cass.agente import AgenteDemorou
from cass.config import carregar_config
from cass.contrato import para_contrato, resposta_de_erro
from cass.modelos import PedidoAgente, RespostaCass

PEDIDO = {"question": "O que mudou no Guaíba?", "hotspot_id": "guaiba-rs", "language": "pt"}
RESPOSTA = RespostaCass(
    tipo="resposta",
    idioma="pt",
    texto="O radar ainda não tem medição.\n\nDados de exemplo.",
    confianca="baixa",
    dados_de_exemplo=True,
)


@pytest.fixture(autouse=True)
def entrada_permitida(monkeypatch: pytest.MonkeyPatch, ids_conhecidos: list[str]) -> None:
    # Estes testes isolam o transporte SSE; a camada de entrada tem testes próprios.
    monkeypatch.setattr(main, "avaliar_entrada", lambda pedido: None)


@pytest.fixture
def cliente(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def filas(monkeypatch: pytest.MonkeyPatch) -> dict[int, asyncio.Queue]:
    """Observa a fila entregue a cada hook, sem depender dos atributos internos dele."""
    por_hook: dict[int, asyncio.Queue] = {}
    original = main.ProgressoERegistro

    def criar(fila: asyncio.Queue | None = None) -> Any:
        hook = original(fila)
        por_hook[id(hook)] = hook.fila
        return hook

    monkeypatch.setattr(main, "ProgressoERegistro", criar)
    return por_hook


def _eventos(texto: str) -> list[tuple[str, dict[str, Any]]]:
    eventos = []
    for bloco in texto.strip().split("\n\n"):
        linhas = bloco.splitlines()
        nome = next(linha.removeprefix("event: ") for linha in linhas if linha.startswith("event:"))
        dados = next(linha.removeprefix("data: ") for linha in linhas if linha.startswith("data:"))
        eventos.append((nome, json.loads(dados)))
    return eventos


def test_stream_termina_no_mesmo_contrato_do_json(
    cliente: TestClient, filas: dict[int, asyncio.Queue], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def responder(
        pedido: PedidoAgente, config: Any, progresso: Any = None, validar: Any = None, andamento: Any = None
    ) -> tuple:
        if progresso is not None:
            fila = filas[id(progresso)]
            fila.put_nowait(("ferramenta", {"nome": "consultar_hotspot"}))
        return RESPOSTA, ["consultar_hotspot"]

    monkeypatch.setattr(main, "responder", responder)

    normal = cliente.post("/api/agente", json=PEDIDO)
    stream = cliente.post("/api/agente/stream", json=PEDIDO)

    assert stream.status_code == normal.status_code == 200
    assert stream.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert stream.headers["cache-control"] == "no-store"
    assert stream.headers["x-accel-buffering"] == "no"
    assert _eventos(stream.text) == [
        ("pensando", {}),
        ("ferramenta", {"nome": "consultar_hotspot"}),
        ("resposta", normal.json()),
    ]
    assert set(normal.json()) == {"paragraphs", "tools", "sources", "note"}


@pytest.mark.parametrize("language", ["pt", "en"])
@pytest.mark.parametrize(
    ("erro", "status"),
    [(AgenteDemorou("limite interno"), 504), (RuntimeError("segredo interno"), 500)],
)
def test_falha_e_evento_terminal_com_resposta_localizada(
    cliente: TestClient,
    filas: dict[int, asyncio.Queue],
    monkeypatch: pytest.MonkeyPatch,
    erro: Exception,
    status: int,
    language: str,
) -> None:
    async def responder(
        pedido: PedidoAgente, config: Any, progresso: Any, validar: Any = None, andamento: Any = None
    ) -> tuple:
        raise erro

    monkeypatch.setattr(main, "responder", responder)

    resposta = cliente.post("/api/agente/stream", json={**PEDIDO, "language": language})

    assert resposta.status_code == 200  # o stream já começou
    assert _eventos(resposta.text) == [
        ("pensando", {}),
        ("erro", {"status": status, "response": resposta_de_erro(language).model_dump()}),
    ]
    assert str(erro) not in resposta.text


def test_resposta_sem_texto_termina_com_erro(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def responder(
        pedido: PedidoAgente, config: Any, progresso: Any, validar: Any = None, andamento: Any = None
    ) -> tuple:
        return RESPOSTA.model_copy(update={"texto": " "}), []

    monkeypatch.setattr(main, "responder", responder)

    resposta = cliente.post("/api/agente/stream", json=PEDIDO)

    assert _eventos(resposta.text) == [
        ("pensando", {}),
        ("erro", {"status": 500, "response": resposta_de_erro("pt").model_dump()})
    ]


@pytest.mark.parametrize(
    "corpo",
    [
        {"language": "pt"},
        {"question": "oi", "language": "es"},
        {"question": "x" * 501, "language": "pt"},
    ],
)
def test_pedido_invalido_e_http_422_antes_de_abrir_stream(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch, corpo: dict
) -> None:
    async def responder(*args: Any, **kwargs: Any) -> tuple:
        pytest.fail("pedido inválido não deve chamar o agente")

    monkeypatch.setattr(main, "responder", responder)

    resposta = cliente.post("/api/agente/stream", json=corpo)

    assert resposta.status_code == 422
    assert resposta.headers["content-type"] == "application/json"


def _scope(versao: str = "2.3") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": versao},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/agente/stream",
        "raw_path": b"/api/agente/stream",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "server": ("teste", 80),
        "client": ("127.0.0.1", 12345),
    }


def _configurar_asgi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main.app.state,
        "config",
        carregar_config({"GOOGLE_CLOUD_PROJECT": "projeto-teste"}),
        raising=False,
    )


def test_asgi_entrega_progresso_antes_de_o_agente_terminar(
    filas: dict[int, asyncio.Queue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _configurar_asgi(monkeypatch)

    async def verificar() -> None:
        liberar = asyncio.Event()
        recebido = asyncio.Event()
        mensagens: list[dict] = []
        entrada: asyncio.Queue[dict] = asyncio.Queue()
        entrada.put_nowait({"type": "http.request", "body": json.dumps(PEDIDO).encode()})

        async def responder(
            pedido: PedidoAgente, config: Any, progresso: Any, validar: Any = None, andamento: Any = None
        ) -> tuple:
            await liberar.wait()
            filas[id(progresso)].put_nowait(("ferramenta", {"nome": "consultar_hotspot"}))
            return RESPOSTA, ["consultar_hotspot"]

        async def enviar(mensagem: dict) -> None:
            mensagens.append(mensagem)
            if b"event: pensando" in mensagem.get("body", b""):
                recebido.set()

        monkeypatch.setattr(main, "responder", responder)
        tarefa = asyncio.create_task(main.app(_scope(), entrada.get, enviar))
        await asyncio.wait_for(recebido.wait(), 1)
        assert not tarefa.done()
        assert not any(b"event: resposta" in m.get("body", b"") for m in mensagens)
        liberar.set()
        await asyncio.wait_for(tarefa, 1)
        corpo = b"".join(m.get("body", b"") for m in mensagens).decode()
        assert _eventos(corpo)[-1] == (
            "resposta", para_contrato(RESPOSTA, ["consultar_hotspot"]).model_dump()
        )
        assert mensagens[-1]["more_body"] is False

    asyncio.run(verificar())


@pytest.mark.parametrize("versao", ["2.3", "2.4"])
def test_desconexao_cancela_e_aguarda_o_agente(
    filas: dict[int, asyncio.Queue], monkeypatch: pytest.MonkeyPatch, versao: str
) -> None:
    _configurar_asgi(monkeypatch)

    async def verificar() -> None:
        recebido = asyncio.Event()
        cancelado = asyncio.Event()
        entrada: asyncio.Queue[dict] = asyncio.Queue()
        entrada.put_nowait({"type": "http.request", "body": json.dumps(PEDIDO).encode()})

        async def responder(
            pedido: PedidoAgente, config: Any, progresso: Any, validar: Any = None, andamento: Any = None
        ) -> tuple:
            filas[id(progresso)].put_nowait(("ferramenta", {"nome": "consultar_hotspot"}))
            try:
                await asyncio.Event().wait()
            finally:
                # O fechamento precisa aguardar também o cleanup assíncrono do agente.
                await asyncio.sleep(0)
                cancelado.set()

        async def enviar(mensagem: dict) -> None:
            if b"event: ferramenta" in mensagem.get("body", b""):
                recebido.set()
                if versao == "2.4":
                    raise OSError("cliente desconectou durante o envio")

        monkeypatch.setattr(main, "responder", responder)
        tarefa = asyncio.create_task(main.app(_scope(versao), entrada.get, enviar))
        await asyncio.wait_for(recebido.wait(), 1)
        if versao == "2.3":
            entrada.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(tarefa, 1)
        else:
            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(tarefa, 1)
        assert cancelado.is_set()

    asyncio.run(verificar())


def test_pedidos_simultaneos_recebem_filas_separadas(
    filas: dict[int, asyncio.Queue], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verificar() -> None:
        ambas = asyncio.Event()

        async def responder(
            pedido: PedidoAgente, config: Any, progresso: Any, validar: Any = None, andamento: Any = None
        ) -> tuple:
            filas[id(progresso)].put_nowait(("ferramenta", {"nome": pedido.question}))
            if len(filas) == 2:
                ambas.set()
            await ambas.wait()
            return RESPOSTA.model_copy(update={"texto": pedido.question}), []

        monkeypatch.setattr(main, "responder", responder)
        config = carregar_config({"GOOGLE_CLOUD_PROJECT": "projeto-teste"})

        async def consumir(pergunta: str) -> list[tuple[str, dict]]:
            pedido = PedidoAgente(question=pergunta, language="pt")
            partes = [parte async for parte in main._eventos_agente(pedido, config)]
            return _eventos("".join(partes))

        primeiro, segundo = await asyncio.wait_for(
            asyncio.gather(consumir("primeiro"), consumir("segundo")), 1
        )
        assert primeiro[:2] == [("pensando", {}), ("ferramenta", {"nome": "primeiro"})]
        assert segundo[:2] == [("pensando", {}), ("ferramenta", {"nome": "segundo"})]
        assert primeiro[-1][1]["paragraphs"] == ["primeiro"]
        assert segundo[-1][1]["paragraphs"] == ["segundo"]
        assert len({id(fila) for fila in filas.values()}) == 2

    asyncio.run(verificar())
