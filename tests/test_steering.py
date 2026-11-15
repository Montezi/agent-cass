"""Steering determinístico e integração com o Agent real, sem Gemini ou rede."""

import asyncio
import json
import threading
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from strands import Agent, tool
from strands.models.model import Model
from strands.vended_plugins.steering import Guide, Proceed

from cass import steering
from cass.guardrails import LimpaDados, PortaoDeFerramentas
from cass.modelos import RespostaDoModelo
from cass.steering import SteeringCass


@pytest.fixture(autouse=True)
def decider_desligado(monkeypatch: pytest.MonkeyPatch) -> Mock:
    decider = Mock(return_value=0.0)
    monkeypatch.setattr(steering, "decider_sim", decider)
    return decider


def chamada(nome: str = "consultar_hotspot", **entrada: Any) -> dict:
    return {"name": nome, "toolUseId": "consulta", "input": entrada}


def candidata(**campos: Any) -> dict:
    return {"role": "assistant", "content": [{"toolUse": {
        "name": "RespostaDoModelo", "toolUseId": "saida", "input": {
            "tipo": "resposta", "texto": "O radar mediu uma mudança.",
            "confianca": "baixa", **campos,
        },
    }}]}


def antes(handler: SteeringCass, uso: dict) -> Proceed | Guide:
    return asyncio.run(handler.steer_before_tool(agent=SimpleNamespace(), tool_use=uso))


def depois(handler: SteeringCass, mensagem: dict, motivo: str = "tool_use") -> Proceed | Guide:
    return asyncio.run(handler.steer_after_model(
        agent=SimpleNamespace(), message=mensagem, stop_reason=motivo
    ))


@pytest.mark.parametrize("pontuacao", [0.0, 0.4999, 0.5, 0.5001, 1.0])
def test_hotspot_sem_gaveta_respeita_o_limiar_e_nao_muda_a_chamada(
    pontuacao: float, decider_desligado: Mock,
) -> None:
    decider_desligado.return_value = pontuacao
    handler = SteeringCass("Como está o lago?", None)
    uso = chamada(id="guaiba-rs")
    original = deepcopy(uso)

    decisao = antes(handler, uso)

    assert isinstance(decisao, Guide if pontuacao < 0.5 else Proceed)
    assert uso == original
    decider_desligado.assert_called_once_with(
        "Como está o lago?", "O usuário deixou claro que quer o hotspot guaiba-rs?",
        padrao=0.0,
    )
    assert handler.negadas == ({"consulta"} if pontuacao < 0.5 else set())
    if isinstance(decisao, Guide):
        assert "pergunta_de_volta" in decisao.reason and "opções" in decisao.reason


@pytest.mark.parametrize("aberto,uso", [
    ("guaiba-rs", chamada(id="guaiba-rs")),
    (None, chamada("listar_hotspots")),
    (None, chamada("skills", skill_name="medido-vs-hipotese")),
])
def test_hotspot_aberto_ou_ferramenta_sem_id_prossegue_sem_decider(
    aberto: str | None, uso: dict, decider_desligado: Mock,
) -> None:
    assert isinstance(antes(SteeringCass("Pergunta.", aberto), uso), Proceed)
    decider_desligado.assert_not_called()


def test_falha_inesperada_do_decider_ainda_pede_esclarecimento(decider_desligado: Mock) -> None:
    decider_desligado.side_effect = OSError("indisponível")
    decisao = antes(SteeringCass("Como está o lago?", None), chamada(id="guaiba-rs"))
    assert isinstance(decisao, Guide)


def test_decider_sincrono_roda_fora_da_thread_do_event_loop(decider_desligado: Mock) -> None:
    principal = threading.get_ident()
    threads = []

    def decidir(*args: Any, **kwargs: Any) -> float:
        threads.append(threading.get_ident())
        return 1.0

    decider_desligado.side_effect = decidir
    assert isinstance(antes(SteeringCass("Quero o Guaíba.", None), chamada(id="guaiba-rs")), Proceed)
    assert threads and threads[0] != principal


@pytest.mark.parametrize("pontuacao", [0.0, 0.4999, 0.5, 0.5001, 1.0])
def test_causalidade_do_decider_so_guia_acima_de_meio(
    pontuacao: float, decider_desligado: Mock,
) -> None:
    decider_desligado.return_value = pontuacao
    decisao = depois(SteeringCass("Pergunta.", "guaiba-rs"), candidata())
    assert isinstance(decisao, Guide if pontuacao > 0.5 else Proceed)
    decider_desligado.assert_called_once_with(
        "O radar mediu uma mudança.", steering.PERGUNTA_CAUSALIDADE, padrao=0.0,
    )


@pytest.mark.parametrize("texto", [
    "O El Niño causou a mudança.", "El Niño caused the flood.",
])
def test_regra_fixa_guia_causalidade_mesmo_com_decider_desligado(texto: str) -> None:
    assert isinstance(depois(SteeringCass("Pergunta.", None), candidata(texto=texto)), Guide)


def test_candidata_estruturada_verifica_texto_medido_e_hipotese(decider_desligado: Mock) -> None:
    mensagem = candidata(
        texto="Texto principal.", medido=["Frase medida."],
        hipotese={"texto": "Hipótese regional.", "evidencia": "Série observada.", "forca": "fraca"},
    )
    original = deepcopy(mensagem)

    assert isinstance(depois(SteeringCass("Pergunta.", None), mensagem), Proceed)
    assert mensagem == original
    decider_desligado.assert_called_once_with(
        "Texto principal.\nFrase medida.\nHipótese regional.\nSérie observada.",
        steering.PERGUNTA_CAUSALIDADE, padrao=0.0,
    )


def test_nao_trata_entrada_de_outra_ferramenta_como_resposta_final(decider_desligado: Mock) -> None:
    mensagem = {"role": "assistant", "content": [{"toolUse": chamada(
        "rascunhar_alerta", id="guaiba-rs", texto="O El Niño causou a mudança.",
    )}]}
    assert isinstance(depois(SteeringCass("Pergunta.", None), mensagem), Proceed)
    decider_desligado.assert_not_called()


@pytest.mark.parametrize("como_json", [False, True])
def test_resposta_final_em_texto_tambem_e_verificada(como_json: bool) -> None:
    texto = "O El Niño causou a mudança."
    if como_json:
        texto = json.dumps({"texto": texto, "medido": [], "hipotese": None})
    mensagem = {"role": "assistant", "content": [{"text": texto}]}
    assert isinstance(depois(SteeringCass("Pergunta.", None), mensagem, "end_turn"), Guide)


def test_causalidade_tem_so_uma_nova_tentativa_por_pergunta(decider_desligado: Mock) -> None:
    handler = SteeringCass("Pergunta.", None)
    mensagem = candidata(texto="O El Niño causou a mudança.")
    assert isinstance(depois(handler, mensagem), Guide)
    assert isinstance(depois(handler, mensagem), Proceed)
    assert handler.correcoes_causalidade == 1
    assert decider_desligado.call_count == 1
    # Um outro pedido não herda a correção anterior.
    assert isinstance(depois(SteeringCass("Outra pergunta.", None), mensagem), Guide)


def test_falha_do_decider_nao_desliga_a_regra_fixa(decider_desligado: Mock) -> None:
    decider_desligado.side_effect = RuntimeError("falhou")
    decisao = depois(SteeringCass("Pergunta.", None), candidata(texto="O El Niño causou a mudança."))
    assert isinstance(decisao, Guide)


def test_agent_real_rejeita_schema_causal_e_ledger_recebe_resultado_limpo() -> None:
    class ModeloLocal(Model):
        def __init__(self) -> None:
            self.chamadas = 0
            self.recebidas = []

        def get_config(self) -> dict:
            return {"model_id": "modelo-local", "context_window_limit": 10000}

        def update_config(self, **model_config: Any) -> None:
            raise AssertionError("Não há reconfiguração neste teste.")

        async def structured_output(self, *args: Any, **kwargs: Any):
            raise AssertionError("O teste usa invoke_async.")
            yield  # pragma: no cover

        async def stream(self, messages: Any, *args: Any, **kwargs: Any):
            self.recebidas.append(deepcopy(messages))
            self.chamadas += 1
            if self.chamadas == 1:
                uso = chamada(id="guaiba-rs")
            else:
                texto = "O El Niño causou a mudança." if self.chamadas == 2 else "Há uma hipótese regional."
                uso = candidata(texto=texto)["content"][0]["toolUse"]
                uso["toolUseId"] = f"saida-{self.chamadas}"
            assert self.chamadas <= 3, "A correção não convergiu."
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {
                "toolUse": {"name": uso["name"], "toolUseId": uso["toolUseId"]}
            }}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
                "toolUse": {"input": json.dumps(uso["input"])}
            }}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            yield {"metadata": {"usage": {
                "inputTokens": 1, "outputTokens": 1, "totalTokens": 2,
            }, "metrics": {"latencyMs": 1}}}

    executadas = []

    @tool
    async def consultar_hotspot(id: str) -> dict:
        """Consulta exclusivamente um dado de teste local."""
        executadas.append(id)
        return {"descricao": "d" * 300, "dados_de_exemplo": True}

    handler = SteeringCass("O que mudou no Guaíba?", "guaiba-rs")
    modelo = ModeloLocal()
    portao = PortaoDeFerramentas()
    agente = Agent(
        model=modelo, tools=[consultar_hotspot], plugins=[handler],
        interventions=[portao, LimpaDados()], callback_handler=None,
    )

    resposta = asyncio.run(agente.invoke_async(
        "O que mudou no Guaíba?", structured_output_model=RespostaDoModelo,
        limits={"structured_output_attempts": 1},
    ))

    assert resposta.stop_reason == "tool_use"
    assert resposta.structured_output.texto == "Há uma hipótese regional."
    assert executadas == ["guaiba-rs"]
    assert modelo.chamadas == 3
    assert portao.chamadas == 1
    assert handler.correcoes_causalidade == 1
    assert not any("saida-2" in str(m) for m in agente.messages)
    assert any(steering.ORIENTACAO_CAUSALIDADE in str(m) for m in modelo.recebidas[2])
    ledger = handler.steering_context.data.get("ledger")
    assert [c["tool_use_id"] for c in ledger["tool_calls"]] == ["consulta", "saida-3"]
    consulta = ledger["tool_calls"][0]
    assert consulta["tool_name"] == "consultar_hotspot"
    assert consulta["tool_args"] == {"id": "guaiba-rs"}
    assert consulta["status"] == "success"
    assert consulta["error"] is None
    assert "timestamp" in consulta and "completion_timestamp" in consulta
    assert json.loads(consulta["result"][0]["text"]) == {
        "descricao": "d" * 200, "dados_de_exemplo": True, "_tipo": "dado",
    }
