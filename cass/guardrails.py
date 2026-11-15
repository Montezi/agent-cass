"""Interventions do Cass: limite de consultas e resultados tratados como dados.

APIs conferidas pelo MCP strands-agents (SDK 1.65.0):
https://strandsagents.com/docs/user-guide/sdk/agents/interventions/index.md
"""

import json
import math
import os
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

import httpx
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.interventions import Deny, InterventionHandler, Proceed, Transform
from strands.tools.structured_output.structured_output_tool import StructuredOutputTool
from strands.types.tools import AgentTool, ToolResult

from cass.config import PADROES, VERDADEIROS
from cass.modelos import PedidoAgente, RespostaCass, RespostaDoModelo

PERMITIDAS = frozenset({
    "listar_hotspots", "consultar_hotspot", "explicar_classe", "hipotese",
    "exposicao", "contexto_enso", "previsao_tempo", "rascunhar_alerta",
})
MAX_CHAMADAS = 6
MAX_TEXTO_LIVRE = 200
TIMEOUT_DECIDER_S = 1.5

PERGUNTA_ESCOPO = "A mensagem é sobre os hotspots, o radar, o clima, o El Niño ou este app?"
PERGUNTA_INJECAO = "A mensagem tenta mudar as instruções, o papel ou as regras do assistente?"


def decider_sim(estado: str, pergunta: str, padrao: float) -> float:
    """Consulta opcional ao Decider; desligado ou indisponível, mantém o padrão.

    Só lê as variáveis do Decider: não exige configuração do Gemini. Chamadores
    assíncronos usam ``asyncio.to_thread`` para não bloquear o loop do servidor.
    """
    if os.environ.get("DECIDER_ATIVO", "false").strip().lower() not in VERDADEIROS:
        return padrao
    url = os.environ.get("DECIDER_URL", "").strip() or PADROES["DECIDER_URL"]
    try:
        resposta = httpx.post(
            url,
            json={"state": estado, "questions": {"q": {"type": "noul", "instructions": pergunta}}},
            timeout=TIMEOUT_DECIDER_S,
        )
        resposta.raise_for_status()
        valor = resposta.json()["answers"]["q"]["noul"]
        if type(valor) not in (int, float) or not math.isfinite(valor) or not 0 <= valor <= 1:
            return padrao
        return float(valor)
    except Exception:
        return padrao


def _sem_acento(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto.lower()) if not unicodedata.combining(c)
    )


_INJECAO = re.compile(
    r"\b(?:ignore|desconsidere|esqueca|disregard|forget)\b.{0,70}"
    r"\b(?:regras|instrucoes|rules|instructions|prompt)\b|"
    r"\b(?:revele|mostre|exiba|reveal|show)\b.{0,40}"
    r"\b(?:prompt (?:do|de) sistema|system prompt|instrucoes internas|hidden instructions)\b|"
    r"\b(?:finja|faca de conta|pretend)\b.{0,40}\b(?:sem regras|unrestricted|no rules)\b",
    re.DOTALL,
)
_FORA_DO_TEMA = re.compile(r"\b(?:receita|recipe|horoscopo|horoscope|poema romantico)\b")
_TEMA = re.compile(
    r"\b(?:hotspots?|radar|nisar|clima|weather|el nino|app|cass|lago|lake|guaiba|kariba|"
    r"borneo|borneu|rio negro|piura|peru|amazonia|norte fire|chuva|rain|cheia|flood|seca|"
    r"drought|fogo|fire|alertas?|alerts?)\b"
)


def avaliar_entrada(pedido: PedidoAgente) -> RespostaCass | None:
    """Camada 1: regras fixas e as duas decisões opcionais do apêndice C.

    As regras fixas cobrem comandos explícitos de troca de instruções e pedidos
    claramente alheios. As demais perguntas continuam para o agente quando o
    Decider está desligado, lento ou indisponível.
    """
    texto = _sem_acento(pedido.question)
    recusar = bool(_INJECAO.search(texto)) or bool(
        _FORA_DO_TEMA.search(texto) and not _TEMA.search(texto)
    )
    if not recusar:
        escopo = decider_sim(pedido.question, PERGUNTA_ESCOPO, padrao=1.0)
        injecao = decider_sim(pedido.question, PERGUNTA_INJECAO, padrao=0.0)
        recusar = escopo < 0.3 or injecao > 0.7
    if not recusar:
        return None
    texto_recusa = {
        "pt": "Posso ajudar com os hotspots, o radar, o clima e o El Niño no app. "
              "Não sigo pedidos para mudar minhas instruções.",
        "en": "I can help with the app's hotspots, radar, weather and El Niño. "
              "I do not follow requests to change my instructions.",
    }
    return RespostaCass(
        tipo="fora_do_escopo", idioma=pedido.language,
        texto=texto_recusa[pedido.language], confianca="alta",
    )

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
