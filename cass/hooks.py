"""Progresso e registro do Cass: observa a execução sem alterar os eventos do Strands."""

import asyncio
import json
from dataclasses import dataclass
from time import monotonic
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from cass.modelos import RespostaDoModelo

EventoProgresso = tuple[str, dict[str, Any]]


@dataclass
class _Chamada:
    nome: str
    inicio: float
    tempo_s: float | None = None


class ProgressoERegistro(HookProvider):
    """Uma instância por pedido, no mesmo loop de ``Agent.invoke_async``.

    A fila é sem limite por padrão. Se o consumidor fornecer uma fila cheia,
    descarta-se o progresso para que a observação nunca bloqueie o agente.
    O registro não contém pergunta, argumentos, resultados nem texto da resposta.
    """

    def __init__(self, fila: asyncio.Queue[EventoProgresso | None] | None = None) -> None:
        self.fila = fila if fila is not None else asyncio.Queue()
        self.finalizar_por_invocacao = True
        self._inicio: float | None = None
        self._chamadas: list[_Chamada] = []
        self._em_curso: dict[str, _Chamada] = {}
        self._finalizado = False

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.antes_da_invocacao)
        registry.add_callback(BeforeToolCallEvent, self.antes_da_ferramenta)
        registry.add_callback(AfterToolCallEvent, self.depois_da_ferramenta)
        registry.add_callback(AfterInvocationEvent, self.depois_da_invocacao)

    def iniciar(self) -> None:
        """Inclui também a preparação do pedido, antes da invocação do modelo."""
        if self._inicio is None:
            self._inicio = monotonic()
            self._publicar("pensando", {})

    def antes_da_invocacao(self, evento: BeforeInvocationEvent) -> None:
        self.iniciar()

    def iniciar_ferramenta(self, id_chamada: str, nome: str) -> None:
        """Observa também a consulta antecipada de hotspot, fora do Agent."""
        self.iniciar()
        chamada = _Chamada(nome=nome, inicio=monotonic())
        self._chamadas.append(chamada)
        self._em_curso[id_chamada] = chamada
        self._publicar("ferramenta", {"nome": nome})

    def concluir_ferramenta(self, id_chamada: str, tempo_s: float | None = None) -> None:
        chamada = self._em_curso.pop(id_chamada, None)
        if chamada is None:
            return
        chamada.tempo_s = tempo_s if tempo_s is not None else monotonic() - chamada.inicio
        if not self._em_curso:
            self._publicar("pensando", {})

    def antes_da_ferramenta(self, evento: BeforeToolCallEvent) -> None:
        # A ferramenta de saída estruturada só formata a resposta, não consulta dados.
        if evento.tool_use["name"] != RespostaDoModelo.__name__:
            self.iniciar_ferramenta(evento.tool_use["toolUseId"], evento.tool_use["name"])

    def depois_da_ferramenta(self, evento: AfterToolCallEvent) -> None:
        self.concluir_ferramenta(evento.tool_use["toolUseId"], evento.duration)

    def depois_da_invocacao(self, evento: AfterInvocationEvent) -> None:
        if not self.finalizar_por_invocacao:
            return
        saida = evento.result.structured_output if evento.result is not None else None
        self.finalizar(getattr(saida, "tipo", "erro"))

    def finalizar(self, tipo: str = "erro") -> None:
        """Imprime uma linha por pedido; também serve para falhas antes do Agent.

        Em chamadas interrompidas, mede até o fim da invocação. As concluídas usam
        a duração fornecida pelo Strands. Chamadas repetidas ficam em linhas de
        ferramenta separadas, na ordem de início, mesmo quando rodam em paralelo.
        """
        if self._finalizado:
            return
        self._finalizado = True
        fim = monotonic()
        registro = {
            "ferramentas": [
                {
                    "nome": c.nome,
                    "tempo_s": round(c.tempo_s if c.tempo_s is not None else fim - c.inicio, 6),
                }
                for c in self._chamadas
            ],
            "tempo_total_s": round(fim - self._inicio, 6) if self._inicio is not None else 0.0,
            "tipo": tipo,
        }
        try:
            print(json.dumps(registro, ensure_ascii=False), flush=True)
        except (OSError, ValueError):
            # A indisponibilidade da saída de logs não pode mudar a resposta.
            pass

    def _publicar(self, evento: str, dados: dict[str, Any]) -> None:
        try:
            self.fila.put_nowait((evento, dados))
        except asyncio.QueueFull:
            pass
