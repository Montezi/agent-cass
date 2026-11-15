"""Correção de rumo do Cass: hotspot claro e causalidade tratada como hipótese.

APIs conferidas no MCP strands-agents para o SDK 1.65.0:
https://strandsagents.com/docs/user-guide/sdk/agents/interventions/steering/index.md
"""

import asyncio
import json
from typing import Any

from strands import Agent
from strands.types.content import Message
from strands.types.streaming import StopReason
from strands.types.tools import ToolUse
from strands.vended_plugins.steering import Guide, LedgerProvider, Proceed, SteeringHandler

from cass.guardrails import decider_sim
from cass.modelos import RespostaDoModelo
from cass.verificacao import afirma_causalidade

PERGUNTA_CAUSALIDADE = "O texto afirma que o El Niño causou a mudança?"
ORIENTACAO_HOTSPOT = (
    "Não adivinhe o hotspot: responda com pergunta_de_volta e as opções disponíveis, "
    "no idioma do pedido. Use listar_hotspots para obter os nomes das opções quando precisar."
)
ORIENTACAO_CAUSALIDADE = (
    "Reescreva sem afirmar que o El Niño causou a mudança. Separe o medido da hipótese, "
    "com evidência e força, no idioma do pedido. Mantenha o formato de RespostaDoModelo."
)


class SteeringCass(SteeringHandler):
    """Uma instância por pergunta; no máximo uma correção da resposta final."""

    name = "steering-cass"

    def __init__(self, conversa: str, hotspot_aberto: str | None) -> None:
        super().__init__(context_providers=[LedgerProvider()])
        self.conversa = conversa
        self.hotspot_aberto = hotspot_aberto
        self.negadas: set[str] = set()
        self.correcoes_causalidade = 0

    async def steer_before_tool(
        self, *, agent: Agent, tool_use: ToolUse, **kwargs: Any
    ) -> Proceed | Guide:
        entrada = tool_use.get("input")
        if self.hotspot_aberto or not isinstance(entrada, dict) or "id" not in entrada:
            return Proceed(reason="A chamada não precisa esclarecer o hotspot.")

        pontuacao = await _decidir(
            self.conversa,
            f"O usuário deixou claro que quer o hotspot {entrada['id']}?",
        )
        if pontuacao < 0.5:
            self.negadas.add(tool_use["toolUseId"])
            return Guide(reason=ORIENTACAO_HOTSPOT)
        return Proceed(reason="O usuário deixou claro qual hotspot deseja.")

    async def steer_after_model(
        self, *, agent: Agent, message: Message, stop_reason: StopReason, **kwargs: Any
    ) -> Proceed | Guide:
        texto = _texto_final(message, stop_reason)
        if texto is None:
            return Proceed(reason="O modelo ainda está consultando ferramentas.")
        if self.correcoes_causalidade >= 1:
            # A verificação posterior do serviço trata uma causalidade persistente.
            return Proceed(reason="A única tentativa de correção já foi solicitada.")

        pontuacao = await _decidir(texto, PERGUNTA_CAUSALIDADE)
        if pontuacao > 0.5 or afirma_causalidade(texto):
            self.correcoes_causalidade += 1
            return Guide(reason=ORIENTACAO_CAUSALIDADE)
        return Proceed(reason="A resposta não afirma causalidade do El Niño.")


async def _decidir(texto: str, pergunta: str) -> float:
    try:
        # O cliente HTTP síncrono não pode impedir o timeout do pedido no event loop.
        return await asyncio.to_thread(decider_sim, texto, pergunta, padrao=0.0)
    except Exception:
        # decider_sim já trata indisponibilidade. Esta proteção também mantém o
        # padrão seguro caso uma implementação alternativa levante uma exceção.
        return 0.0


def _texto_final(mensagem: Message, motivo: StopReason) -> str | None:
    """Lê a candidata final, incluindo a saída estruturada que termina em tool_use."""
    conteudo = mensagem.get("content", [])
    candidatas = [
        bloco["toolUse"].get("input")
        for bloco in conteudo
        if "toolUse" in bloco and bloco["toolUse"].get("name") == RespostaDoModelo.__name__
    ]
    if motivo != "end_turn" and not candidatas:
        return None

    textos = []
    for bloco in conteudo:
        if isinstance(bloco.get("text"), str):
            texto = bloco["text"]
            try:
                valor = json.loads(texto)
            except ValueError:
                textos.append(texto)
            else:
                # A resposta forçada também pode terminar como JSON em texto.
                textos.extend(_textos_estruturados(valor) if isinstance(valor, dict) else [texto])
    for candidata in candidatas:
        textos.extend(_textos_estruturados(candidata))
    return "\n".join(textos)


def _textos_estruturados(valor: Any) -> list[str]:
    if not isinstance(valor, dict):
        return []
    textos = [valor["texto"]] if isinstance(valor.get("texto"), str) else []
    medido = valor.get("medido")
    if isinstance(medido, list):
        textos.extend(frase for frase in medido if isinstance(frase, str))
    hipotese = valor.get("hipotese")
    if isinstance(hipotese, dict):
        textos.extend(
            hipotese[campo] for campo in ("texto", "evidencia")
            if isinstance(hipotese.get(campo), str)
        )
    return textos
