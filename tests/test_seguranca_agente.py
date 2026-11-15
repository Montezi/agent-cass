"""Regressões de integridade da saída e custo, com SDK real e modelo local."""

import asyncio

import pytest
from strands import tool

from cass import agente
from cass.config import carregar_config
from cass.modelos import Hipotese, PedidoAgente, RespostaCass
from cass.verificacao import validar_resposta
from test_guardrails_integracao import ModeloProgramado, chamada


def resposta(**campos):
    return RespostaCass(tipo="resposta", idioma="pt", texto="Veja os dados disponíveis.",
                        confianca="baixa", **campos)


@pytest.mark.parametrize("campos", [
    {"opcoes": ["Área inundada: 999 km²"]},
    {"opcoes": ["O El Niño causou a cheia."]},
    {"hipotese": Hipotese(texto="Possível chuva de 999 mm.", evidencia="Observação.", forca="fraca")},
    {"hipotese": Hipotese(texto="Possível chuva.", evidencia="Registro de 999 mm.", forca="fraca")},
])
def test_campos_publicados_nao_escapam_da_verificacao(campos):
    assert not validar_resposta(resposta(**campos), [])


def test_numero_confirmado_em_opcao_e_hipotese_continua_valido():
    r = resposta(opcoes=["Área inundada: 212 km²"], hipotese=Hipotese(
        texto="Possível mudança da área.", evidencia="Medição de 212 km².", forca="fraca",
    ))
    assert validar_resposta(r, [("consultar_hotspot", {"affected_area_km2": 212})])


def test_nome_exato_com_digitos_nao_e_medicao_e_nao_libera_area():
    dados = [("listar_hotspots", {"hotspots": [{"name": {"pt": "Lago 12", "en": "Lake 12"}}]})]
    assert validar_resposta(resposta(opcoes=["Lago 12"]), dados)
    assert not validar_resposta(resposta(opcoes=["Lago 12: 12 km²"]), dados)


def preparar(monkeypatch, modelo):
    @tool
    async def listar_hotspots() -> dict:
        """Lista local para provar que o limite de consultas não limita chamadas ao modelo."""
        return {"hotspots": []}

    monkeypatch.setenv("DECIDER_ATIVO", "false")
    monkeypatch.setattr(agente, "criar_modelo", lambda config: modelo)
    monkeypatch.setattr(agente, "carregar_skills", lambda: ())
    monkeypatch.setattr(agente, "FERRAMENTAS_ATIVAS", [listar_hotspots])
    return PedidoAgente(question="Quais hotspots estão disponíveis?", language="pt")


def test_loop_de_ferramentas_para_em_oito_turnos_mesmo_apos_recusas(monkeypatch):
    modelo = ModeloProgramado([[chamada("listar_hotspots", f"lista-{i}")] for i in range(9)])
    pedido = preparar(monkeypatch, modelo)

    with pytest.raises(agente.AgenteSemResposta, match="limit_turns"):
        asyncio.run(agente.responder(pedido, carregar_config({"GOOGLE_CLOUD_PROJECT": "teste"})))

    assert len(modelo.recebidas) == 8
    assert len(modelo.roteiro) == 1


def test_correcao_nao_renova_orcamento_de_turnos(monkeypatch):
    roteiro = [[chamada("listar_hotspots", f"lista-{i}")] for i in range(7)]
    roteiro.append([chamada("RespostaDoModelo", "saida", tipo="resposta",
                            texto="A área é 999 km².", confianca="alta")])
    roteiro.append([chamada("RespostaDoModelo", "saida-extra", tipo="resposta",
                            texto="Sem medições disponíveis.", confianca="baixa")])
    modelo = ModeloProgramado(roteiro)
    pedido = preparar(monkeypatch, modelo)

    with pytest.raises(agente.AgenteSemResposta, match="limit_turns"):
        asyncio.run(agente.responder(
            pedido, carregar_config({"GOOGLE_CLOUD_PROJECT": "teste"}), validar=validar_resposta,
        ))

    assert len(modelo.recebidas) == 8
    assert len(modelo.roteiro) == 1


@pytest.mark.parametrize("limite,valor,motivo", [
    ("MAX_TOKENS_GERADOS", 1, "limit_output_tokens"),
    ("MAX_TOKENS_TOTAIS", 2, "limit_total_tokens"),
])
def test_orcamento_de_tokens_interrompe_novas_chamadas(monkeypatch, limite, valor, motivo):
    modelo = ModeloProgramado([[chamada("listar_hotspots", f"lista-{i}")] for i in range(2)])
    pedido = preparar(monkeypatch, modelo)
    monkeypatch.setattr(agente, limite, valor)

    with pytest.raises(agente.AgenteSemResposta, match=motivo):
        asyncio.run(agente.responder(pedido, carregar_config({"GOOGLE_CLOUD_PROJECT": "teste"})))

    assert len(modelo.recebidas) == 1
