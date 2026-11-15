"""Admissão HTTP, CORS e camadas compartilhadas entre JSON e SSE, sem Gemini."""

import asyncio
import json
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from cass import main
from cass.config import carregar_config
from cass.limites import LimitadorPorIP, LimiteDeCorpo, ip_do_cliente, verificar_limite_ip
from cass.modelos import PedidoAgente, RespostaCass

ORIGEM = "https://app.teste"
PEDIDO = {"question": "O que mudou no Guaíba?", "hotspot_id": "guaiba-rs", "language": "pt"}
RESPOSTA = RespostaCass(
    tipo="resposta", idioma="pt", texto="Ainda não há medição disponível.", confianca="baixa"
)
ROTAS = ["/api/agente", "/api/agente/stream"]


@pytest.fixture
def chamadas(monkeypatch: pytest.MonkeyPatch, ids_conhecidos: list[str]) -> list[dict[str, Any]]:
    recebidas: list[dict[str, Any]] = []

    async def responder(
        pedido: PedidoAgente, config: Any, progresso: Any = None, validar: Any = None
    ) -> tuple:
        recebidas.append({"pedido": pedido, "progresso": progresso, "validar": validar})
        return RESPOSTA.model_copy(update={"idioma": pedido.language}), []

    monkeypatch.setattr(main, "avaliar_entrada", lambda pedido: None)
    monkeypatch.setattr(main, "responder", responder)
    return recebidas


@pytest.fixture
def cliente(
    monkeypatch: pytest.MonkeyPatch, chamadas: list[dict[str, Any]]
) -> Iterator[TestClient]:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    monkeypatch.setenv("APP_ORIGIN", ORIGEM)
    with TestClient(main.app) as c:
        yield c


def _dados_terminais(resposta: Any) -> dict[str, Any]:
    if resposta.headers["content-type"].startswith("application/json"):
        return resposta.json()
    blocos = resposta.text.strip().split("\n\n")
    linhas = blocos[-1].splitlines()
    assert linhas[0] == "event: resposta"
    return json.loads(next(linha[6:] for linha in linhas if linha.startswith("data: ")))


@pytest.mark.parametrize("rota", ROTAS)
def test_camadas_sao_compartilhadas_e_callback_de_verificacao_e_passado(
    cliente: TestClient, chamadas: list[dict[str, Any]], rota: str
) -> None:
    resposta = cliente.post(rota, json=PEDIDO)

    assert resposta.status_code == 200
    assert set(_dados_terminais(resposta)) == {"paragraphs", "tools", "sources", "note"}
    assert len(chamadas) == 1
    assert chamadas[0]["validar"] is main.validar_resposta
    assert chamadas[0]["progresso"] is not None


@pytest.mark.parametrize("rota", ROTAS)
@pytest.mark.parametrize("language", ["pt", "en"])
def test_recusa_de_entrada_nao_chama_o_agente_e_mantem_o_contrato(
    cliente: TestClient,
    chamadas: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rota: str,
    language: str,
) -> None:
    texto = "Consigo falar sobre os hotspots." if language == "pt" else "I can discuss the hotspots."
    recusa = RespostaCass(
        tipo="fora_do_escopo", idioma=language, texto=texto, confianca="baixa"
    )
    monkeypatch.setattr(main, "avaliar_entrada", lambda pedido: recusa)

    resposta = cliente.post(rota, json={**PEDIDO, "language": language})

    assert resposta.status_code == 200
    assert _dados_terminais(resposta) == {
        "paragraphs": [texto], "tools": [], "sources": [], "note": None
    }
    assert chamadas == []
    registros = [json.loads(linha) for linha in capsys.readouterr().out.splitlines()]
    assert len(registros) == 1
    assert registros[0]["tipo"] == "fora_do_escopo"


@pytest.mark.parametrize("rota", ROTAS)
def test_tempo_da_camada_de_entrada_tambem_conta_no_limite_total(
    cliente: TestClient,
    chamadas: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    rota: str,
) -> None:
    def avaliar(pedido: PedidoAgente) -> None:
        time.sleep(0.1)

    monkeypatch.setattr(main, "avaliar_entrada", avaliar)
    monkeypatch.setattr(main, "TEMPO_LIMITE_S", 0.02)

    resposta = cliente.post(rota, json=PEDIDO)

    if rota.endswith("/stream"):
        assert resposta.status_code == 200
        assert "event: erro" in resposta.text
        assert '"status": 504' in resposta.text
    else:
        assert resposta.status_code == 504
    assert chamadas == []


def test_json_e_sse_compartilham_as_dez_perguntas_por_ip(
    cliente: TestClient, chamadas: list[dict[str, Any]]
) -> None:
    for numero in range(10):
        assert cliente.post(ROTAS[numero % 2], json=PEDIDO).status_code == 200

    for rota in ROTAS:
        resposta = cliente.post(rota, json=PEDIDO)
        assert resposta.status_code == 429
        assert resposta.headers["content-type"] == "application/json"
        assert 1 <= int(resposta.headers["retry-after"]) <= 60
        assert set(resposta.json()) == {"paragraphs", "tools", "sources", "note"}
        assert resposta.json()["tools"] == []
    assert len(chamadas) == 10


def test_limite_e_aplicado_antes_da_entrada_inclusive_para_fora_do_escopo(
    cliente: TestClient,
    chamadas: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    avaliadas: list[str] = []

    def avaliar(pedido: PedidoAgente) -> RespostaCass:
        avaliadas.append(pedido.question)
        return RESPOSTA.model_copy(update={"tipo": "fora_do_escopo"})

    monkeypatch.setattr(main, "avaliar_entrada", avaliar)
    for _ in range(10):
        assert cliente.post("/api/agente", json=PEDIDO).status_code == 200

    resposta = cliente.post("/api/agente/stream", json=PEDIDO)

    assert resposta.status_code == 429
    assert len(avaliadas) == 10
    assert chamadas == []


def test_cota_segue_o_primeiro_endereco_do_x_forwarded_for(cliente: TestClient) -> None:
    # No Cloud Run: "cliente, proxy do Google". O proxy pode variar; o cliente não.
    for numero in range(10):
        resposta = cliente.post(
            "/api/agente", json=PEDIDO, headers={"X-Forwarded-For": f"203.0.113.7, 10.0.0.{numero}"}
        )
        assert resposta.status_code == 200

    mesmo_cliente = cliente.post("/api/agente", json=PEDIDO, headers={"X-Forwarded-For": "203.0.113.7"})
    outro_cliente = cliente.post("/api/agente", json=PEDIDO, headers={"X-Forwarded-For": "203.0.113.8"})

    assert mesmo_cliente.status_code == 429
    assert outro_cliente.status_code == 200


def test_x_forwarded_for_falsificado_ganha_cota_nova(cliente: TestClient) -> None:
    # Limitação conhecida e documentada: o cabeçalho vem de quem chama. O teto de
    # custo real é o --max-instances do Cloud Run, não este limite.
    for numero in range(12):
        resposta = cliente.post(
            "/api/agente", json=PEDIDO, headers={"X-Forwarded-For": f"198.51.100.{numero}"}
        )
        assert resposta.status_code == 200


def requisicao_com(cabecalhos: dict[str, str], cliente: str | None = "10.1.2.3") -> Request:
    return Request({
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in cabecalhos.items()],
        "client": (cliente, 12345) if cliente is not None else None,
    })


@pytest.mark.parametrize(
    ("cabecalhos", "esperado"),
    [
        pytest.param({}, "10.1.2.3", id="sem-cabecalho"),
        pytest.param({"X-Forwarded-For": "203.0.113.7"}, "203.0.113.7", id="um-endereco"),
        pytest.param({"X-Forwarded-For": "203.0.113.7, 34.1.2.3"}, "203.0.113.7", id="com-proxy"),
        pytest.param({"X-Forwarded-For": " 2001:db8::1 ,34.1.2.3"}, "2001:db8::1", id="ipv6"),
        pytest.param({"X-Forwarded-For": ""}, "10.1.2.3", id="vazio"),
        pytest.param({"X-Forwarded-For": "lixo, 203.0.113.7"}, "10.1.2.3", id="primeiro-nao-e-ip"),
    ],
)
def test_ip_do_cliente(cabecalhos: dict[str, str], esperado: str) -> None:
    assert ip_do_cliente(requisicao_com(cabecalhos)) == esperado


def test_ip_do_cliente_sem_cabecalho_nem_conexao() -> None:
    assert ip_do_cliente(requisicao_com({}, cliente=None)) == "desconhecido"


def test_429_e_localizado(cliente: TestClient) -> None:
    for _ in range(10):
        cliente.post("/api/agente", json=PEDIDO)

    resposta = cliente.post("/api/agente/stream", json={**PEDIDO, "language": "en"})

    assert resposta.status_code == 429
    assert "Please wait" in resposta.json()["paragraphs"][0]


def test_cors_so_permite_a_origem_configurada_e_preflight_nao_consume_cota(
    cliente: TestClient,
) -> None:
    for _ in range(12):
        resposta = cliente.options(
            "/api/agente/stream",
            headers={
                "Origin": ORIGEM,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,accept",
            },
        )
        assert resposta.status_code == 200
        assert resposta.headers["access-control-allow-origin"] == ORIGEM
        assert set(resposta.headers["access-control-allow-methods"].split(", ")) == {"GET", "POST"}
        assert "access-control-allow-credentials" not in resposta.headers
        assert "*" not in resposta.headers["access-control-allow-headers"]
    assert cliente.post("/api/agente", json=PEDIDO).status_code == 200

    proibida = cliente.options(
        "/api/agente", headers={"Origin": "https://outra.teste", "Access-Control-Request-Method": "POST"}
    )
    assert proibida.status_code == 400
    assert "access-control-allow-origin" not in proibida.headers


@pytest.mark.parametrize("origem", [ORIGEM, "https://outra.teste"])
def test_cors_tambem_cobre_health_sem_cota(cliente: TestClient, origem: str) -> None:
    for _ in range(12):
        resposta = cliente.get("/health", headers={"Origin": origem})
        assert resposta.status_code == 200
        if origem == ORIGEM:
            assert resposta.headers["access-control-allow-origin"] == ORIGEM
        else:
            assert "access-control-allow-origin" not in resposta.headers
    assert cliente.post("/api/agente", json=PEDIDO).status_code == 200


@pytest.mark.parametrize("status", [422, 429, 500])
def test_cors_cobre_erros_e_expoe_retry_after(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    corpo = PEDIDO
    if status == 422:
        corpo = {"question": "oi", "language": "es"}
    elif status == 429:
        for _ in range(10):
            cliente.post("/api/agente", json=PEDIDO)
    else:
        async def falhar(*args: Any, **kwargs: Any) -> tuple:
            raise RuntimeError("falha simulada")
        monkeypatch.setattr(main, "responder", falhar)

    resposta = cliente.post("/api/agente", json=corpo, headers={"Origin": ORIGEM})

    assert resposta.status_code == status
    assert resposta.headers["access-control-allow-origin"] == ORIGEM
    assert resposta.headers["access-control-expose-headers"] == "Retry-After"


def test_cors_muda_com_a_config_do_lifespan_e_nao_aceita_wildcard(
    monkeypatch: pytest.MonkeyPatch, chamadas: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    for origem in ["https://primeira.teste", "https://segunda.teste", "*"]:
        monkeypatch.setenv("APP_ORIGIN", origem)
        with TestClient(main.app) as cliente:
            resposta = cliente.get("/health", headers={"Origin": "https://primeira.teste"})
            if origem == "https://primeira.teste":
                assert resposta.headers["access-control-allow-origin"] == origem
            else:
                assert "access-control-allow-origin" not in resposta.headers


def test_expiracao_libera_cotas_e_mapa_cheio_nao_esquece_ips_ativos() -> None:
    agora = [10.0]
    limite = LimitadorPorIP(limite=1, max_ips=2, relogio=lambda: agora[0])
    assert limite.admitir("primeiro") is None
    agora[0] = 20.0
    assert limite.admitir("segundo") is None
    assert limite.admitir("terceiro") == 50  # mapa cheio: aguarda a janela mais antiga
    assert limite.admitir("primeiro") == 50  # a recusa não removeu o IP já esgotado
    agora[0] = 70.0
    assert limite.admitir("terceiro") is None
    assert limite.admitir("segundo") == 10
    agora[0] = 80.0
    assert limite.admitir("segundo") is None
    assert limite.admitir("primeiro") == 50


def test_ips_distintos_e_ausencia_de_cliente_tem_cotas_separadas_sem_lifespan() -> None:
    app = SimpleNamespace(state=SimpleNamespace(config=carregar_config({"GOOGLE_CLOUD_PROJECT": "p"})))

    def requisicao(ip: str | None) -> Request:
        return Request({
            "type": "http", "app": app, "headers": [],
            "client": (ip, 12345) if ip is not None else None,
        })

    for ip in ["203.0.113.1", "203.0.113.2", None]:
        for _ in range(10):
            assert verificar_limite_ip(requisicao(ip)) is None
        assert verificar_limite_ip(requisicao(ip)) is not None
    # Trocar a config em um teste ASGI equivale a iniciar um novo ciclo de vida.
    app.state.config = carregar_config({"GOOGLE_CLOUD_PROJECT": "outra-config"})
    assert verificar_limite_ip(requisicao("203.0.113.1")) is None


# ---------- corpo do pedido de no máximo 2 KB (api-contract.md do frontend) ----------


def corpo_com(tamanho: int, **campos: Any) -> bytes:
    """JSON válido do pedido, completado com espaços até `tamanho` bytes."""
    texto = json.dumps({**PEDIDO, **campos}, ensure_ascii=False).encode()
    assert len(texto) <= tamanho
    return texto + b" " * (tamanho - len(texto))


def postar(cliente: TestClient, rota: str, corpo: bytes, **cabecalhos: str) -> Any:
    return cliente.post(
        rota, content=corpo, headers={"Content-Type": "application/json", **cabecalhos}
    )


@pytest.mark.parametrize("rota", ROTAS)
def test_corpo_de_ate_2_kb_passa(
    cliente: TestClient, chamadas: list[dict[str, Any]], rota: str
) -> None:
    assert postar(cliente, rota, corpo_com(2048)).status_code == 200
    assert len(chamadas) == 1


@pytest.mark.parametrize("rota", ROTAS)
def test_corpo_acima_de_2_kb_e_recusado_antes_do_agente(
    cliente: TestClient, chamadas: list[dict[str, Any]], rota: str
) -> None:
    resposta = postar(cliente, rota, corpo_com(2049))

    assert resposta.status_code == 413
    assert resposta.headers["content-type"] == "application/json"
    assert "2048 bytes" in resposta.json()["detail"]
    assert chamadas == []


def test_pergunta_maxima_do_app_cabe_em_2_kb(
    cliente: TestClient, chamadas: list[dict[str, Any]]
) -> None:
    # 500 caracteres de 3 bytes em UTF-8 (o pior caso comum) ainda cabem.
    pergunta = "水" * 500

    resposta = postar(cliente, "/api/agente", json.dumps({**PEDIDO, "question": pergunta}, ensure_ascii=False).encode())

    assert resposta.status_code == 200
    assert chamadas[0]["pedido"].question == pergunta


def test_recusa_413_tem_cors_para_a_origem_do_app(cliente: TestClient) -> None:
    resposta = postar(cliente, "/api/agente", corpo_com(4096), Origin=ORIGEM)

    assert resposta.status_code == 413
    assert resposta.headers["access-control-allow-origin"] == ORIGEM


def test_413_nao_consome_a_cota_por_ip(
    cliente: TestClient, chamadas: list[dict[str, Any]]
) -> None:
    for _ in range(12):
        assert postar(cliente, "/api/agente", corpo_com(3000)).status_code == 413

    assert postar(cliente, "/api/agente", corpo_com(200)).status_code == 200


def _rodar_asgi(app: Any, scope: dict[str, Any], pedacos: list[bytes]) -> list[dict[str, Any]]:
    mensagens = [
        {"type": "http.request", "body": pedaco, "more_body": i < len(pedacos) - 1}
        for i, pedaco in enumerate(pedacos)
    ]
    enviadas: list[dict[str, Any]] = []

    async def receber() -> dict[str, Any]:
        return mensagens.pop(0) if mensagens else {"type": "http.disconnect"}

    async def enviar(mensagem: dict[str, Any]) -> None:
        enviadas.append(mensagem)

    asyncio.run(app(scope, receber, enviar))
    return enviadas


def _escopo(caminho: str = "/api/agente", metodo: str = "POST") -> dict[str, Any]:
    # Sem Content-Length: o corpo chega em pedaços.
    return {"type": "http", "method": metodo, "path": caminho, "headers": []}


def test_corpo_em_pedacos_sem_content_length_tambem_e_contado() -> None:
    recebidos: list[bytes] = []

    async def aplicacao(scope: Any, receive: Any, send: Any) -> None:
        recebidos.append((await receive())["body"])

    enviadas = _rodar_asgi(LimiteDeCorpo(aplicacao), _escopo(), [b"x" * 1000] * 3)

    assert enviadas[0]["status"] == 413
    assert recebidos == []


def test_corpo_em_pedacos_dentro_do_limite_chega_inteiro() -> None:
    recebidos: list[dict[str, Any]] = []

    async def aplicacao(scope: Any, receive: Any, send: Any) -> None:
        recebidos.append(await receive())
        recebidos.append(await receive())  # depois do corpo, o desconectar original

    _rodar_asgi(LimiteDeCorpo(aplicacao), _escopo(), [b"a" * 1000, b"b" * 1000])

    assert recebidos[0] == {"type": "http.request", "body": b"a" * 1000 + b"b" * 1000, "more_body": False}
    assert recebidos[1] == {"type": "http.disconnect"}


@pytest.mark.parametrize(
    ("caminho", "metodo"),
    [("/health", "GET"), ("/outra", "POST"), ("/api/agente", "OPTIONS")],
)
def test_outras_rotas_e_metodos_passam_direto(caminho: str, metodo: str) -> None:
    chamada: list[str] = []

    async def aplicacao(scope: Any, receive: Any, send: Any) -> None:
        chamada.append(scope["path"])

    _rodar_asgi(LimiteDeCorpo(aplicacao), _escopo(caminho, metodo), [b"x" * 5000])

    assert chamada == [caminho]
