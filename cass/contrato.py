"""Converte a RespostaCass (formato interno) para o contrato público do /api/agente.

O contrato (docs/api-contract.md) só tem paragraphs, tools, sources e note.
Os campos da RespostaCass sem lugar nele estão em docs/propostas-api-contract.md.
"""

import re

from cass.formato import AVISO_DE_EXEMPLO, Idioma, formatar_fonte
from cass.modelos import (
    MAX_CARACTERES_FERRAMENTA,
    MAX_CARACTERES_FONTE,
    MAX_CARACTERES_NOTA,
    MAX_CARACTERES_PARAGRAFO,
    MAX_FERRAMENTAS,
    MAX_FONTES,
    MAX_PARAGRAFOS,
    RespostaAgente,
    RespostaCass,
    tamanho_js,
)

ROTULOS: dict[Idioma, dict[str, str]] = {
    "pt": {
        "medido": "Medido: {frases}.",
        "hipotese": "Hipótese (força {forca}): {texto}. Evidência: {evidencia}.",
        "opcoes": "Opções: {opcoes}.",
        "erro": "Não consegui responder agora. Tente de novo em instantes.",
    },
    "en": {
        "medido": "Measured: {frases}.",
        "hipotese": "Hypothesis ({forca} strength): {texto}. Evidence: {evidencia}.",
        "opcoes": "Options: {opcoes}.",
        "erro": "I couldn't answer right now. Please try again in a moment.",
    },
}
FORCAS: dict[Idioma, dict[str, str]] = {
    "pt": {"fraca": "fraca", "moderada": "moderada", "forte": "forte"},
    "en": {"fraca": "weak", "moderada": "moderate", "forte": "strong"},
}


def para_contrato(resposta: RespostaCass, ferramentas: list[str]) -> RespostaAgente:
    """RespostaCass -> RespostaAgente. `ferramentas` é o que o código viu ser usado.

    Aplica os limites do contrato (até 12 parágrafos de 2.000 caracteres, 10
    ferramentas de 60 sem repetição, 10 fontes de 200 e nota de 500): o excedente
    do texto se junta no último parágrafo do texto, e o que passar do tamanho é
    cortado com "…". Medido, Hipótese e Opções ficam sempre em parágrafos próprios.

    Levanta ValueError se não sobrar nenhum parágrafo (o contrato exige pelo menos um).
    """
    rotulos = ROTULOS[resposta.idioma]
    texto = [p.strip() for p in re.split(r"\n\s*\n", resposta.texto) if p.strip()]
    if not texto:
        raise ValueError("a RespostaCass veio sem texto")

    extras: list[str] = []
    frases = [_sem_ponto(m) for m in resposta.medido if m.strip()]
    if frases:
        extras.append(rotulos["medido"].format(frases="; ".join(frases)))
    if resposta.hipotese:
        extras.append(
            rotulos["hipotese"].format(
                forca=FORCAS[resposta.idioma][resposta.hipotese.forca],
                texto=_sem_ponto(resposta.hipotese.texto),
                evidencia=_sem_ponto(resposta.hipotese.evidencia),
            )
        )
    if resposta.opcoes:
        extras.append(rotulos["opcoes"].format(opcoes=", ".join(resposta.opcoes)))

    vagas = MAX_PARAGRAFOS - len(extras)  # extras são no máximo 3
    if len(texto) > vagas:
        texto = [*texto[: vagas - 1], " ".join(texto[vagas - 1 :])]
    paragrafos = [_cortar(p, MAX_CARACTERES_PARAGRAFO) for p in [*texto, *extras]]

    # As fontes já vêm montadas pelo código (cass/agente.py), não pelo modelo.
    fontes = [formatar_fonte(f.nome, f.data, resposta.idioma) for f in resposta.fontes]
    nota = AVISO_DE_EXEMPLO[resposta.idioma] if resposta.dados_de_exemplo else None
    return RespostaAgente(
        paragraphs=paragrafos,
        tools=_primeiros_sem_repetir(ferramentas, MAX_CARACTERES_FERRAMENTA, MAX_FERRAMENTAS),
        sources=_primeiros_sem_repetir(fontes, MAX_CARACTERES_FONTE, MAX_FONTES),
        note=_cortar(nota, MAX_CARACTERES_NOTA) if nota else None,
    )


def resposta_de_erro(idioma: Idioma) -> RespostaAgente:
    """Corpo das respostas 500 e 504, no formato do contrato."""
    return RespostaAgente(paragraphs=[ROTULOS[idioma]["erro"]], tools=[], sources=[])


def _sem_ponto(frase: str) -> str:
    return frase.strip().rstrip(".").strip()


def _primeiros_sem_repetir(itens: list[str], caracteres: int, quantos: int) -> list[str]:
    return list(dict.fromkeys(_cortar(i, caracteres) for i in itens))[:quantos]


def _cortar(texto: str, limite: int) -> str:
    """Corta em `limite` unidades UTF-16 (como o zod conta), terminando com "…".

    Prefere cortar num espaço perto do fim e não deixa um ** de negrito aberto.
    """
    if tamanho_js(texto) <= limite:
        return texto
    caracteres: list[str] = []
    usado = 0
    for c in texto:
        tamanho = 2 if ord(c) > 0xFFFF else 1
        if usado + tamanho > limite - 1:  # 1 para o "…"
            break
        caracteres.append(c)
        usado += tamanho
    cortado = "".join(caracteres)
    espaco = cortado.rfind(" ")
    if espaco >= 0.8 * limite:
        cortado = cortado[:espaco]
    if cortado.count("**") % 2:
        aberto = cortado.rfind("**")
        cortado = cortado[:aberto] + cortado[aberto + 2 :]
    return cortado.rstrip() + "…"
