"""Interventions no Agent real, com respostas de modelo programadas e sem rede."""

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest
from strands import Agent, AgentSkills, Skill, tool
from strands.models.model import Model

from cass import agente as modulo_agente
from cass.config import carregar_config
from cass.guardrails import LimpaDados, PortaoDeFerramentas
from cass.modelos import RespostaDoModelo


def chamada(nome: str, identificador: str, **entrada: Any) -> dict:
    return {"name": nome, "toolUseId": identificador, "input": entrada}


def saida_final() -> dict:
    return chamada(
        "RespostaDoModelo", "saida", tipo="resposta", texto="Consultei os dados disponíveis.",
        confianca="baixa",
    )


class ModeloProgramado(Model):
    """Emite toolUse válidos; execução, bloqueio e limpeza ficam a cargo do SDK."""

    def __init__(self, roteiro: list[list[dict]]) -> None:
        self.roteiro = list(roteiro)
        self.recebidas: list[list[dict]] = []

    def get_config(self) -> dict:
        return {"model_id": "modelo-local", "context_window_limit": 10000}

    def update_config(self, **model_config: Any) -> None:
        raise AssertionError("O modelo local não precisa de reconfiguração.")

    async def structured_output(self, *args: Any, **kwargs: Any):
        raise AssertionError("O teste usa invoke_async, sem a API obsoleta.")
        yield  # pragma: no cover

    async def stream(self, messages: Any, *args: Any, **kwargs: Any):
        self.recebidas.append(deepcopy(messages))
        assert self.roteiro, "O Agent chamou o modelo além do roteiro esperado."
        yield {"messageStart": {"role": "assistant"}}
        for indice, uso in enumerate(self.roteiro.pop(0)):
            yield {"contentBlockStart": {"contentBlockIndex": indice, "start": {
                "toolUse": {"toolUseId": uso["toolUseId"], "name": uso["name"]}
            }}}
            yield {"contentBlockDelta": {"contentBlockIndex": indice, "delta": {
                "toolUse": {"input": json.dumps(uso["input"])}
            }}}
            yield {"contentBlockStop": {"contentBlockIndex": indice}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {
            "inputTokens": 1, "outputTokens": 1, "totalTokens": 2
        }, "metrics": {"latencyMs": 1}}}


def resultados(mensagens: list[dict]) -> dict[str, dict]:
    return {
        bloco["toolResult"]["toolUseId"]: bloco["toolResult"]
        for mensagem in mensagens
        for bloco in mensagem.get("content", [])
        if "toolResult" in bloco
    }


def invocar(agente: Agent) -> Any:
    return asyncio.run(agente.invoke_async(
        "Consulte os dados locais.", structured_output_model=RespostaDoModelo
    ))


def test_criar_agente_bloqueia_setima_chamada_e_limpa_antes_de_voltar_ao_modelo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {
        "descricao": "Texto livre. " * 40,
        "hotspot": {
            "id": "guaiba-rs", "affected_area_km2": 212.5,
            "confidence": 0.87, "last_pass": "2026-11-12",
            "name": {"pt": "Nome muito extenso. " * 30},
        },
        "fonte": {"nome": "Fonte " * 50, "data": "2026-11-12", "url": "https://dados.teste"},
        "dados_de_exemplo": True,
    }
    antes = deepcopy(original)
    executadas: list[str] = []

    @tool
    async def consultar_hotspot(id: str) -> dict:
        """Consulta um dado local para verificar o limite de execução."""
        executadas.append(id)
        return original

    @tool
    async def previsao_tempo(id: str) -> dict:
        """Esta consulta existe, mas já deve estar fora da cota."""
        pytest.fail("A sétima consulta não pode executar.")

    consultas = [chamada("consultar_hotspot", f"consulta-{i}", id=str(i)) for i in range(6)]
    consultas.append(chamada("previsao_tempo", "consulta-6", id="guaiba-rs"))
    modelo = ModeloProgramado([consultas, [saida_final()]])
    monkeypatch.setattr(modulo_agente, "criar_modelo", lambda config: modelo)
    monkeypatch.setattr(modulo_agente, "carregar_skills", lambda: ())
    monkeypatch.setattr(modulo_agente, "FERRAMENTAS_ATIVAS", [consultar_hotspot, previsao_tempo])
    config = carregar_config({"GOOGLE_CLOUD_PROJECT": "teste-local"})
    portao, limpa = PortaoDeFerramentas(), LimpaDados()
    agente = modulo_agente.criar_agente(config, portao=portao, limpa_dados=limpa)

    resposta = invocar(agente)

    assert isinstance(resposta.structured_output, RespostaDoModelo)
    assert resposta.structured_output.tipo == "resposta"
    assert executadas == [str(i) for i in range(6)]
    assert portao.chamadas == 7
    assert portao.negadas == {"consulta-6"}
    assert modelo.roteiro == []
    assert original == antes
    recebidos = resultados(modelo.recebidas[1])
    for i in range(6):
        retorno = recebidos[f"consulta-{i}"]
        assert retorno["status"] == "success"
        limpo = json.loads(retorno["content"][0]["text"])
        assert limpo["_tipo"] == "dado"
        assert limpo["descricao"] == original["descricao"][:200]
        assert limpo["hotspot"]["name"]["pt"] == original["hotspot"]["name"]["pt"][:200]
        assert limpo["hotspot"]["affected_area_km2"] == 212.5
        assert limpo["hotspot"]["confidence"] == 0.87
        assert limpo["hotspot"]["last_pass"] == "2026-11-12"
        assert limpo["dados_de_exemplo"] is True
        assert limpo["fonte"] == original["fonte"]
    assert recebidos["consulta-6"]["status"] == "error"
    assert "Limite de 6" in recebidos["consulta-6"]["content"][0]["text"]
    # O extrator usado pelo serviço continua lendo JSON, fonte e aviso de exemplo.
    extraidos = modulo_agente._resultados_das_ferramentas(agente.messages, portao.negadas)
    assert len(extraidos) == 6
    assert modulo_agente._ferramentas_chamadas(agente.messages, portao.negadas) == ["consultar_hotspot"]
    fontes = modulo_agente._fontes(extraidos)
    assert len(fontes) == 6
    assert fontes[0].nome == original["fonte"]["nome"]
    assert fontes[0].data == "2026-11-12"


def test_nome_desconhecido_e_bloqueado_mesmo_quando_a_ferramenta_existe() -> None:
    executadas = []

    @tool
    async def executar_comando() -> str:
        """Registra uma execução que deve ser impedida pelo portão."""
        executadas.append(True)
        return "Esta ferramenta não deve executar."

    modelo = ModeloProgramado([
        [chamada("executar_comando", "proibida")], [saida_final()]
    ])
    portao = PortaoDeFerramentas()
    agente = Agent(
        model=modelo, tools=[executar_comando],
        interventions=[portao, LimpaDados()], callback_handler=None,
    )

    resposta = invocar(agente)

    assert executadas == []
    assert resposta.structured_output.tipo == "resposta"
    assert portao.chamadas == 1
    retorno = resultados(agente.messages)["proibida"]
    assert retorno["status"] == "error"
    assert "Ferramenta não permitida" in retorno["content"][0]["text"]


def test_skill_real_mantem_instrucoes_completas_depois_da_quota() -> None:
    instrucoes = "Instruções locais confiáveis para a skill de teste. " * 15
    plugin = AgentSkills(skills=[Skill(
        name="teste-integracao", description="Skill local para integração.",
        instructions=instrucoes,
    )], strict=True)
    portao, limpa = PortaoDeFerramentas(), LimpaDados()
    portao.registrar_ferramentas_internas(plugin.tools)
    limpa.registrar_ferramentas_internas(plugin.tools)
    executadas = []

    @tool
    async def listar_hotspots() -> dict:
        """Retorna um resultado local sem consultar a rede."""
        executadas.append(True)
        return {"hotspots": []}

    modelo = ModeloProgramado([
        [chamada("listar_hotspots", f"consulta-{i}") for i in range(6)],
        [chamada("skills", "ativar-skill", skill_name="teste-integracao")],
        [saida_final()],
    ])
    agente = Agent(
        model=modelo, tools=[listar_hotspots], plugins=[plugin],
        interventions=[portao, limpa], callback_handler=None,
    )

    resposta = invocar(agente)

    assert resposta.structured_output.tipo == "resposta"
    assert len(executadas) == 6
    assert portao.chamadas == 6
    retornos = resultados(agente.messages)
    assert retornos["ativar-skill"]["status"] == "success"
    assert retornos["ativar-skill"]["content"][0]["text"] == instrucoes
    assert len(instrucoes) > 200
    assert "_tipo" not in retornos["saida"]["content"][0]["text"]
    assert agente.state.get("agent_skills")["activated_skills"] == ["teste-integracao"]


def test_chamada_direta_pelo_agent_tambem_passa_pelas_interventions() -> None:
    executadas = []

    @tool
    async def listar_hotspots() -> dict:
        """Retorna dados de teste para uma chamada direta pelo Agent."""
        executadas.append(True)
        return {"descricao": "x" * 300}

    modelo = ModeloProgramado([])
    portao = PortaoDeFerramentas(max_chamadas=1)
    agente = Agent(
        model=modelo, tools=[listar_hotspots],
        interventions=[portao, LimpaDados()], callback_handler=None,
    )

    primeira = agente.tool.listar_hotspots()
    segunda = agente.tool.listar_hotspots()

    assert executadas == [True]
    assert modelo.recebidas == []
    assert primeira["status"] == "success"
    assert json.loads(primeira["content"][0]["text"]) == {
        "descricao": "x" * 200, "_tipo": "dado",
    }
    assert segunda["status"] == "error"
    assert "Limite de 1" in segunda["content"][0]["text"]
