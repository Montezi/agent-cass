"""Progresso e registro com os eventos e o registro reais do Strands, sem rede."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from strands import Agent, tool
from strands.agent.agent_result import AgentResult
from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookRegistry,
)
from strands.models.model import Model
from strands.telemetry.metrics import EventLoopMetrics

from cass import hooks
from cass.hooks import ProgressoERegistro
from cass.modelos import RespostaDoModelo


@pytest.fixture
def relogio(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    agora = [10.0]
    monkeypatch.setattr(hooks, "monotonic", lambda: agora[0])
    return agora


def registro_do_hook(hook: ProgressoERegistro) -> HookRegistry:
    registro = HookRegistry()
    registro.add_hook(hook)
    return registro


def chamada(identificador: str = "chamada-1", nome: str = "consultar_hotspot") -> dict:
    return {"toolUseId": identificador, "name": nome, "input": {"id": "guaiba-rs"}}


def antes(agente: Any, uso: dict) -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=agente, selected_tool=None, tool_use=uso, invocation_state={}
    )


def depois(agente: Any, uso: dict, duracao: float | None = None) -> AfterToolCallEvent:
    return AfterToolCallEvent(
        agent=agente,
        selected_tool=None,
        tool_use=uso,
        invocation_state={},
        result={"toolUseId": uso["toolUseId"], "status": "success", "content": []},
        duration=duracao,
    )


def resultado(tipo: str = "resposta") -> AgentResult:
    return AgentResult(
        stop_reason="end_turn",
        message={"role": "assistant", "content": [{"text": "Texto privado da resposta."}]},
        metrics=EventLoopMetrics(),
        state={},
        structured_output=RespostaDoModelo(
            tipo=tipo, texto="Texto privado da resposta.", confianca="alta"
        ),
    )


def esvaziar(hook: ProgressoERegistro) -> list:
    eventos = []
    while not hook.fila.empty():
        eventos.append(hook.fila.get_nowait())
    return eventos


def test_eventos_reais_observam_sem_mudar_a_invocacao(
    relogio: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    hook = ProgressoERegistro()
    registro = registro_do_hook(hook)
    mensagens = [{"role": "user", "content": [{"text": "Pergunta privada."}]}]
    agente = SimpleNamespace(messages=mensagens)
    estado = {"pedido": {"id": "privado"}}
    uso = chamada()
    inicio = BeforeInvocationEvent(
        agent=agente, messages=mensagens, invocation_state=estado, cancel="não alterar"
    )
    pre = antes(agente, uso)
    pre.cancel_tool = "preservar cancelamento"
    pos = depois(agente, uso, 1.25)
    pos.retry = True
    resposta = resultado("pergunta_de_volta")
    fim = AfterInvocationEvent(
        agent=agente, result=resposta, invocation_state=estado, resume="preservar retomada"
    )
    originais = deepcopy((mensagens, estado, uso, pos.result, resposta.structured_output))

    async def executar() -> None:
        await registro.invoke_callbacks_async(inicio)
        relogio[0] = 11.0
        await registro.invoke_callbacks_async(pre)
        relogio[0] = 14.0
        await registro.invoke_callbacks_async(pos)
        relogio[0] = 16.0
        await registro.invoke_callbacks_async(fim)
        hook.finalizar("erro")  # A proteção do serviço não duplica o registro.

    asyncio.run(executar())

    assert (mensagens, estado, uso, pos.result, resposta.structured_output) == originais
    assert agente.messages is mensagens and inicio.messages is mensagens
    assert pre.tool_use is uso and pos.tool_use is uso
    assert fim.result is resposta
    assert inicio.cancel == "não alterar"
    assert pre.cancel_tool == "preservar cancelamento"
    assert pos.retry is True
    assert fim.resume == "preservar retomada"
    assert esvaziar(hook) == [
        ("pensando", {}),
        ("ferramenta", {"nome": "consultar_hotspot"}),
        ("pensando", {}),
    ]
    linhas = capsys.readouterr().out.splitlines()
    assert len(linhas) == 1
    assert json.loads(linhas[0]) == {
        "ferramentas": [{"nome": "consultar_hotspot", "tempo_s": 1.25}],
        "tempo_total_s": 6.0,
        "tipo": "pergunta_de_volta",
    }
    assert "privad" not in linhas[0]


def test_chamadas_paralelas_e_repetidas_preservam_os_tempos_por_id(
    relogio: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    hook = ProgressoERegistro()
    registro = registro_do_hook(hook)
    agente = SimpleNamespace(messages=[])
    primeira, segunda = chamada("primeira"), chamada("segunda")

    async def executar() -> None:
        await registro.invoke_callbacks_async(BeforeInvocationEvent(agent=agente))
        relogio[0] = 11.0
        await registro.invoke_callbacks_async(antes(agente, primeira))
        relogio[0] = 12.0
        await registro.invoke_callbacks_async(antes(agente, segunda))
        relogio[0] = 14.0
        await registro.invoke_callbacks_async(depois(agente, segunda, 0.75))
        # A primeira ainda está rodando: concluir a segunda não anuncia "pensando".
        assert esvaziar(hook) == [
            ("pensando", {}),
            ("ferramenta", {"nome": "consultar_hotspot"}),
            ("ferramenta", {"nome": "consultar_hotspot"}),
        ]
        await registro.invoke_callbacks_async(depois(agente, primeira, 2.5))
        relogio[0] = 15.0
        # Uma nova tentativa pode reutilizar o toolUseId depois de terminar.
        await registro.invoke_callbacks_async(antes(agente, primeira))
        relogio[0] = 16.0
        await registro.invoke_callbacks_async(depois(agente, primeira))
        relogio[0] = 18.0
        await registro.invoke_callbacks_async(
            AfterInvocationEvent(agent=agente, result=resultado())
        )

    asyncio.run(executar())

    assert esvaziar(hook) == [
        ("pensando", {}),
        ("ferramenta", {"nome": "consultar_hotspot"}),
        ("pensando", {}),
    ]
    dados = json.loads(capsys.readouterr().out)
    assert dados["ferramentas"] == [
        {"nome": "consultar_hotspot", "tempo_s": 2.5},
        {"nome": "consultar_hotspot", "tempo_s": 0.75},
        {"nome": "consultar_hotspot", "tempo_s": 1.0},
    ]
    assert dados["tempo_total_s"] == 8.0


def test_falha_sem_resultado_mede_chamada_interrompida_ate_o_fim(
    relogio: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    hook = ProgressoERegistro()
    registro = registro_do_hook(hook)
    agente = SimpleNamespace(messages=[])

    async def executar() -> None:
        await registro.invoke_callbacks_async(BeforeInvocationEvent(agent=agente))
        relogio[0] = 12.0
        await registro.invoke_callbacks_async(antes(agente, chamada()))
        relogio[0] = 17.0
        await registro.invoke_callbacks_async(AfterInvocationEvent(agent=agente))

    asyncio.run(executar())

    assert json.loads(capsys.readouterr().out) == {
        "ferramentas": [{"nome": "consultar_hotspot", "tempo_s": 5.0}],
        "tempo_total_s": 7.0,
        "tipo": "erro",
    }


def test_consulta_antecipada_pertence_ao_mesmo_registro(
    relogio: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    hook = ProgressoERegistro()
    registro = registro_do_hook(hook)
    agente = SimpleNamespace(messages=[])
    hook.iniciar()
    relogio[0] = 11.0
    hook.iniciar_ferramenta("prefetch", "consultar_hotspot")
    relogio[0] = 12.0
    hook.concluir_ferramenta("prefetch")

    async def executar() -> None:
        await registro.invoke_callbacks_async(BeforeInvocationEvent(agent=agente))
        relogio[0] = 15.0
        await registro.invoke_callbacks_async(
            AfterInvocationEvent(agent=agente, result=resultado())
        )

    asyncio.run(executar())

    assert esvaziar(hook) == [
        ("pensando", {}),
        ("ferramenta", {"nome": "consultar_hotspot"}),
        ("pensando", {}),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "ferramentas": [{"nome": "consultar_hotspot", "tempo_s": 1.0}],
        "tempo_total_s": 5.0,
        "tipo": "resposta",
    }


@pytest.mark.parametrize("falha", [BrokenPipeError, ValueError])
def test_fila_cheia_e_saida_indisponivel_nao_interrompem_hooks(
    falha: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    fila = asyncio.Queue(maxsize=1)
    fila.put_nowait(("pensando", {}))
    hook = ProgressoERegistro(fila)
    registro = registro_do_hook(hook)
    agente = SimpleNamespace(messages=[])

    def imprimir(*args: Any, **kwargs: Any) -> None:
        raise falha("saída indisponível")

    monkeypatch.setattr("builtins.print", imprimir)

    async def executar() -> None:
        await registro.invoke_callbacks_async(BeforeInvocationEvent(agent=agente))
        await registro.invoke_callbacks_async(antes(agente, chamada()))
        await registro.invoke_callbacks_async(depois(agente, chamada(), 0.0))
        await registro.invoke_callbacks_async(
            AfterInvocationEvent(agent=agente, result=resultado())
        )
        hook.finalizar()

    asyncio.run(executar())
    assert fila.qsize() == 1


def test_agent_real_publica_progresso_e_omite_ferramenta_de_saida(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ModeloLocal(Model):
        """Simula duas respostas de modelo; o Agent executa as ferramentas reais."""

        def __init__(self) -> None:
            self.chamadas = 0

        def get_config(self) -> dict:
            return {"model_id": "modelo-local", "context_window_limit": 10000}

        def update_config(self, **model_config: Any) -> None:
            raise AssertionError("O teste não precisa reconfigurar o modelo.")

        async def structured_output(self, *args: Any, **kwargs: Any):
            raise AssertionError("O fluxo usa invoke_async, sem a API obsoleta.")
            yield  # pragma: no cover

        async def stream(self, messages: Any, *args: Any, **kwargs: Any):
            self.chamadas += 1
            nome = "consultar_local" if self.chamadas == 1 else "RespostaDoModelo"
            dados = {} if self.chamadas == 1 else {
                "tipo": "fora_do_escopo", "texto": "Consigo falar dos hotspots.",
                "confianca": "alta",
            }
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {
                "toolUse": {"toolUseId": f"local-{self.chamadas}", "name": nome}
            }}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
                "toolUse": {"input": json.dumps(dados)}
            }}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            yield {"metadata": {"usage": {
                "inputTokens": 1, "outputTokens": 1, "totalTokens": 2
            }, "metrics": {"latencyMs": 1}}}

    @tool
    async def consultar_local() -> dict:
        """Consulta apenas um dado de teste local."""
        return {"fonte": "teste"}

    hook = ProgressoERegistro()
    modelo = ModeloLocal()
    agente = Agent(
        model=modelo, tools=[consultar_local], hooks=[hook], callback_handler=None
    )
    resposta = asyncio.run(
        agente.invoke_async("Pergunta de teste", structured_output_model=RespostaDoModelo)
    )

    assert resposta.structured_output.tipo == "fora_do_escopo"
    assert modelo.chamadas == 2
    assert esvaziar(hook) == [
        ("pensando", {}),
        ("ferramenta", {"nome": "consultar_local"}),
        ("pensando", {}),
    ]
    linhas = capsys.readouterr().out.splitlines()
    assert len(linhas) == 1
    dados = json.loads(linhas[0])
    assert dados["tipo"] == "fora_do_escopo"
    assert [f["nome"] for f in dados["ferramentas"]] == ["consultar_local"]
    assert dados["ferramentas"][0]["tempo_s"] >= 0
    assert dados["tempo_total_s"] >= dados["ferramentas"][0]["tempo_s"]
