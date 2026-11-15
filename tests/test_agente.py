"""Testes do agente: montagem (sem rede) e uma pergunta real ao Gemini."""

import json
import re
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from google import genai
from strands.hooks import BeforeInvocationEvent

from cass import ferramentas, main
from cass import agente as modulo_agente
from cass.agente import (
    FERRAMENTAS_ATIVAS,
    FERRAMENTAS_DO_CASS,
    PROMPT_DE_SISTEMA,
    TEMPERATURA,
    criar_agente,
    criar_modelo,
)
from cass.ferramentas import explicar_classe, exposicao, hipotese
from cass.config import Configuracao, carregar_config
from cass.hooks import ProgressoERegistro
from cass.modelos import RespostaAgente

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


def test_modelo_limita_o_pensamento_e_a_saida(config: Configuracao) -> None:
    params = criar_modelo(config).get_config()["params"]

    assert params["thinking_config"] == {"thinking_level": "low"}
    assert params["max_output_tokens"] == 4096


def test_orcamento_do_pedido_cabe_duas_chamadas_no_teto() -> None:
    # A resposta e a correção podem, cada uma, usar o teto inteiro de uma chamada.
    from cass.agente import MAX_TOKENS_DE_SAIDA, MAX_TOKENS_GERADOS

    assert MAX_TOKENS_GERADOS >= 2 * MAX_TOKENS_DE_SAIDA


def test_parametros_viram_um_generate_content_config_valido(config: Configuracao) -> None:
    # O GeminiModel repassa params ao GenerateContentConfig; um nome errado quebraria aqui.
    from google.genai import types

    gerado = types.GenerateContentConfig(**criar_modelo(config).get_config()["params"])

    assert gerado.thinking_config is not None
    assert gerado.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert gerado.max_output_tokens == 4096


def test_modelo_e_reaproveitado(config: Configuracao) -> None:
    assert criar_modelo(config) is criar_modelo(config)


def test_agente_registra_so_as_ferramentas_ativas_e_a_das_skills(config: Configuracao) -> None:
    agente = criar_agente(config)

    assert sorted(agente.tool_names) == [
        "consultar_hotspot",
        "contexto_enso",
        "listar_hotspots",
        "previsao_tempo",
        "rascunhar_alerta",
        "skills",  # do plugin AgentSkills: só ativa uma skill pelo nome
    ]


def test_ferramentas_sem_stats_existem_mas_ficam_fora_do_agente(config: Configuracao) -> None:
    nomes = set(criar_agente(config).tool_names)

    for ferramenta in (explicar_classe, hipotese, exposicao):
        assert ferramenta in FERRAMENTAS_DO_CASS
        assert ferramenta not in FERRAMENTAS_ATIVAS
        assert ferramenta.tool_name not in nomes


def test_agente_registra_o_hook_do_pedido(config: Configuracao) -> None:
    progresso = ProgressoERegistro()

    agente = criar_agente(config, progresso=progresso)
    evento = BeforeInvocationEvent(agent=agente)

    assert progresso.antes_da_invocacao in agente.hooks.get_callbacks_for(evento)


def test_agente_tem_o_prompt_do_apendice_c(config: Configuracao) -> None:
    assert criar_agente(config).system_prompt == PROMPT_DE_SISTEMA.format(idioma="português")
    assert criar_agente(config, "en").system_prompt.endswith(
        "Responda em inglês. Comece pelo que o radar mediu (número, data e fonte). "
        "Não escreva aviso de dados de exemplo: o código já o coloca no campo note."
    )
    # O aviso de dados de exemplo é do código (note), não do modelo.
    assert "diga isso" not in PROMPT_DE_SISTEMA


@pytest.mark.parametrize("idioma", ["pt", "en"])
def test_prompt_proibe_numeros_de_conhecimento_geral_e_arredondar(
    config: Configuracao, idioma: str
) -> None:
    regra = (
        "- Não cite anos, prazos ou números de conhecimento geral (como cheias passadas "
        "ou o intervalo de passagens do radar); não arredonde nem converta unidades."
    )

    assert regra in criar_agente(config, idioma).system_prompt
    assert criar_modelo(config).get_config()["params"]["temperature"] == 0.2


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
def test_api_agente_responde_sobre_o_guaiba(
    dados_servidos: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for nome, valor in dotenv_values(RAIZ / ".env").items():
        if valor:
            monkeypatch.setenv(nome, valor)
    monkeypatch.setenv("DADOS_BASE", dados_servidos)
    ferramentas.limpar_cache()

    # Como o frontend chama (agentApi.ts).
    with TestClient(main.app) as cliente:
        http = cliente.post(
            "/api/agente",
            json={"question": "O que mudou no Guaíba?", "hotspot_id": "guaiba-rs", "language": "pt"},
        )
    print(f"\n--- resposta ---\n{json.dumps(http.json(), ensure_ascii=False, indent=2)}\n")

    assert http.status_code == 200
    resposta = RespostaAgente.model_validate(http.json())
    assert "consultar_hotspot" in resposta.tools
    # Só existe porque o hotspot buscado veio do fixture (metadata.sample = true).
    assert resposta.note is not None and resposta.note.startswith("Dados de exemplo")
    # Fontes montadas pelo código: o fixture não tem last_pass.
    assert resposta.sources[0] == "NISAR, data não informada"
    # Nenhuma ferramenta tem dados de hipótese ainda.
    assert not any(p.startswith("Hipótese") for p in resposta.paragraphs)
    # O fixture não tem nenhum valor medido: qualquer área citada seria inventada.
    for paragrafo in resposta.paragraphs:
        assert not re.search(r"\d\s*km", paragrafo), paragrafo


@pytest.mark.gemini
def test_pedir_alerta_leva_a_skill_alerta_whatsapp(
    dados_servidos: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for nome, valor in dotenv_values(RAIZ / ".env").items():
        if valor:
            monkeypatch.setenv(nome, valor)
    monkeypatch.setenv("DADOS_BASE", dados_servidos)
    # 404 em todas as fontes da previsão: indisponível, sem rede externa.
    monkeypatch.setattr(ferramentas, "URL_METEOMATICS", f"{dados_servidos}/meteomatics")
    monkeypatch.setattr(ferramentas, "URL_OPEN_METEO", f"{dados_servidos}/open-meteo")
    ferramentas.limpar_cache()
    criados = []
    original = modulo_agente.criar_agente

    def capturar(*args, **kwargs):  # type: ignore[no-untyped-def]
        criados.append(original(*args, **kwargs))
        return criados[-1]

    monkeypatch.setattr(modulo_agente, "criar_agente", capturar)

    with TestClient(main.app) as cliente:
        http = cliente.post(
            "/api/agente",
            json={"question": "Monte o alerta para o Rio Negro", "hotspot_id": "norte-amazonia", "language": "pt"},
        )
    print(f"\n--- resposta ---\n{json.dumps(http.json(), ensure_ascii=False, indent=2)}\n")

    assert http.status_code == 200
    chamadas = [
        bloco["toolUse"]
        for mensagem in criados[0].messages
        for bloco in mensagem["content"]
        if "toolUse" in bloco
    ]
    # "alerta" na pergunta: a skill vai na mensagem, sem gastar uma chamada para ativá-la.
    primeira = criados[0].messages[0]["content"][0]["text"]
    assert primeira.startswith("Instruções da skill alerta-whatsapp")
    skills_ativadas = [c["input"].get("skill_name") for c in chamadas if c["name"] == "skills"]
    assert "alerta-whatsapp" not in skills_ativadas
    assert "rascunhar_alerta" in RespostaAgente.model_validate(http.json()).tools
