"""Interventions do Cass: limite de consultas e resultados tratados como dados.

APIs conferidas pelo MCP strands-agents (SDK 1.65.0):
https://strandsagents.com/docs/user-guide/sdk/agents/interventions/index.md
"""

import json
from collections.abc import Iterable
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.interventions import Deny, InterventionHandler, Proceed, Transform
from strands.tools.structured_output.structured_output_tool import StructuredOutputTool
from strands.types.tools import AgentTool, ToolResult

from cass.modelos import RespostaDoModelo

PERMITIDAS = frozenset({
    "listar_hotspots", "consultar_hotspot", "explicar_classe", "hipotese",
    "exposicao", "contexto_enso", "previsao_tempo", "rascunhar_alerta",
})
MAX_CHAMADAS = 6
MAX_TEXTO_LIVRE = 200

# Identificadores, caminhos, enums e proveniência não são texto livre. Cortá-los
# mudaria o significado dos dados ou impediria a conferência das fontes.
_CAMPOS_TECNICOS = frozenset({
    "id", "hotspot_id", "validos", "toolUseId", "status", "type", "tipo",
    "date", "data", "last_pass", "updated_at", "atualizada_em",
    "url", "uri", "link", "path", "caminho", "layers",
    "dominant_class", "classe", "strength", "forca", "confianca",
    "confidence", "language", "idioma", "source", "ferramenta", "_tipo",
})
_PROVENIENCIA = frozenset({"fonte", "fontes", "sources"})


class _InterventionDoCass(InterventionHandler):
    def __init__(self) -> None:
        self._internas: tuple[AgentTool, ...] = ()

    def registrar_ferramentas_internas(self, ferramentas: Iterable[AgentTool]) -> None:
        """Confia apenas nos objetos do plugin de skills criado para este pedido."""
        self._internas = tuple(ferramentas)

    def _e_interna(self, evento: BeforeToolCallEvent | AfterToolCallEvent) -> bool:
        selecionada = evento.selected_tool
        if selecionada is None or evento.tool_use["name"] != selecionada.tool_name:
            return False
        if any(selecionada is ferramenta for ferramenta in self._internas):
            return True
        # O modelo precisa conseguir formatar a resposta mesmo após seis consultas.
        # O nome sozinho não autoriza uma ferramenta que imite o schema.
        return (
            isinstance(selecionada, StructuredOutputTool)
            and selecionada.structured_output_model is RespostaDoModelo
        )


class PortaoDeFerramentas(_InterventionDoCass):
    """Uma instância por pergunta, incluindo a consulta antecipada de hotspot.

    Cada tentativa consome uma posição, mesmo quando a ferramenta falha ou é
    recusada. Skills locais e a formatação da saída são infraestrutura; não são
    consultas aos dados e não consomem as seis posições.
    """

    name = "portao-ferramentas"

    def __init__(self, max_chamadas: int = MAX_CHAMADAS) -> None:
        super().__init__()
        if not 1 <= max_chamadas <= MAX_CHAMADAS:
            raise ValueError(f"max_chamadas deve estar entre 1 e {MAX_CHAMADAS}")
        self.max_chamadas = max_chamadas
        self.chamadas = 0
        self.negadas: set[str] = set()

    def autorizar(self, nome: str) -> Proceed | Deny:
        """Mesma regra para a ferramenta do Agent e a consulta antecipada."""
        self.chamadas += 1
        if nome not in PERMITIDAS:
            return Deny(reason="Ferramenta não permitida.")
        if self.chamadas > self.max_chamadas:
            return Deny(reason=f"Limite de {self.max_chamadas} consultas por pergunta atingido.")
        return Proceed()

    def before_tool_call(self, evento: BeforeToolCallEvent, **kwargs: Any) -> Proceed | Deny:
        if self._e_interna(evento):
            return Proceed()
        decisao = self.autorizar(evento.tool_use["name"])
        if isinstance(decisao, Deny):
            self.negadas.add(evento.tool_use["toolUseId"])
        return decisao


class LimpaDados(_InterventionDoCass):
    """Limita o texto recebido das ferramentas, preservando o JSON e as medições.

    A marca ``_tipo: dado`` reforça a regra do prompt: resultados não são
    instruções. Essa marca e o truncamento não detectam toda injeção de prompt.
    """

    name = "limpa-dados"

    def limpar_dados(self, valor: Any) -> dict[str, Any]:
        """Devolve uma cópia marcada; nunca altera o resultado/cache da ferramenta."""
        limpo = _limitar_textos(valor)
        if isinstance(limpo, dict):
            return {**limpo, "_tipo": "dado"}
        return {"_tipo": "dado", "valor": limpo}

    def after_tool_call(self, evento: AfterToolCallEvent, **kwargs: Any) -> Proceed | Transform:
        if self._e_interna(evento):
            return Proceed()

        def aplicar(alvo: AfterToolCallEvent) -> None:
            alvo.result = self._limpar_resultado(alvo.result)

        return Transform(apply=aplicar, reason="Resultado de ferramenta tratado como dado.")

    def _limpar_resultado(self, resultado: ToolResult) -> ToolResult:
        conteudo = []
        for bloco in resultado.get("content", []):
            if isinstance(bloco.get("text"), str):
                texto = bloco["text"]
                try:
                    valor = json.loads(texto)
                except ValueError:
                    valor = texto
                conteudo.append({**bloco, "text": json.dumps(self.limpar_dados(valor), ensure_ascii=False)})
            elif "json" in bloco:
                conteudo.append({**bloco, "json": self.limpar_dados(bloco["json"])})
            else:
                conteudo.append(dict(bloco))
        return {**resultado, "content": conteudo}


def _limitar_textos(valor: Any) -> Any:
    if isinstance(valor, str):
        return valor[:MAX_TEXTO_LIVRE]
    if isinstance(valor, dict):
        return {
            chave: _copiar(dado) if chave in _CAMPOS_TECNICOS | _PROVENIENCIA else _limitar_textos(dado)
            for chave, dado in valor.items()
        }
    if isinstance(valor, list):
        return [_limitar_textos(item) for item in valor]
    return valor


def _copiar(valor: Any) -> Any:
    """Copia também as subárvores preservadas, para não compartilhar o cache."""
    if isinstance(valor, dict):
        return {chave: _copiar(dado) for chave, dado in valor.items()}
    if isinstance(valor, list):
        return [_copiar(item) for item in valor]
    return valor
