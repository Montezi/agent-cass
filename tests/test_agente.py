"""Testes do agente: montagem (sem rede) e uma pergunta real ao Gemini."""

import re
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from dotenv import dotenv_values
from google import genai

from cass import ferramentas
from cass.agente import PROMPT_DE_SISTEMA, TEMPERATURA, criar_agente, criar_modelo
from cass.config import Configuracao, carregar_config

RAIZ = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def config() -> Configuracao:
    return carregar_config(
        {"GOOGLE_CLOUD_PROJECT": "projeto-teste", "GEMINI_MODEL": "gemini-teste"}
    )


# ---------- montagem, sem rede ----------


def test_modelo_usa_vertex_ai_com_o_projeto_da_config(config: Configuracao) -> None:
    modelo = criar_modelo(config)

    cliente = modelo._custom_client
    assert isinstance(cliente, genai.Client)
    assert cliente.vertexai is True
    assert cliente._api_client.project == "projeto-teste"
    assert cliente._api_client.location == "global"


def test_modelo_usa_gemini_model_e_temperatura(config: Configuracao) -> None:
    modelo_config = criar_modelo(config).get_config()

    assert modelo_config["model_id"] == "gemini-teste"
    assert modelo_config["params"]["temperature"] == TEMPERATURA == 0.2


def test_modelo_e_reaproveitado(config: Configuracao) -> None:
    assert criar_modelo(config) is criar_modelo(config)


def test_agente_tem_so_as_duas_ferramentas(config: Configuracao) -> None:
    agente = criar_agente(config)

    assert sorted(agente.tool_names) == ["consultar_hotspot", "listar_hotspots"]


def test_agente_tem_o_prompt_do_apendice_c(config: Configuracao) -> None:
    assert criar_agente(config).system_prompt == PROMPT_DE_SISTEMA.format(idioma="português")
    assert criar_agente(config, "en").system_prompt.endswith(
        "Responda em inglês. Se os dados forem de exemplo, diga isso."
    )


def test_cada_pedido_ganha_um_agente_novo(config: Configuracao) -> None:
    # O Agent guarda a conversa e não aceita duas chamadas ao mesmo tempo.
    assert criar_agente(config) is not criar_agente(config)


# ---------- pergunta real ao Gemini ----------


@pytest.fixture
def dados_servidos() -> Iterator[str]:
    """Serve tests/fixtures por HTTP numa porta livre, como o frontend serve /dados."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{servidor.server_address[1]}"
    servidor.shutdown()


@pytest.mark.gemini
def test_pergunta_sobre_o_guaiba(
    dados_servidos: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for nome, valor in dotenv_values(RAIZ / ".env").items():
        if valor:
            monkeypatch.setenv(nome, valor)
    monkeypatch.setenv("DADOS_BASE", dados_servidos)
    ferramentas.limpar_cache()

    agente = criar_agente(carregar_config())
    texto = str(agente("O que mudou no Guaíba?")).strip()
    print(f"\n--- resposta do Cass ---\n{texto}\n")

    chamadas = [
        bloco["toolUse"]
        for mensagem in agente.messages
        for bloco in mensagem["content"]
        if "toolUse" in bloco
    ]
    assert {"name": "consultar_hotspot", "id": "guaiba-rs"} in [
        {"name": c["name"], "id": c["input"].get("id")} for c in chamadas
    ]
    assert "exemplo" in texto.lower()
    # O fixture não tem nenhum valor medido: qualquer área citada seria inventada.
    assert not re.search(r"\d\s*km", texto)
