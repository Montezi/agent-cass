"""Converte a RespostaCass (formato interno) para o contrato público do /api/agente.

O contrato (docs/api-contract.md) só tem paragraphs, tools, sources e note.
Os campos da RespostaCass sem lugar nele estão em docs/propostas-api-contract.md.
"""

import re
from typing import Literal

from cass.modelos import RespostaAgente, RespostaCass

Idioma = Literal["pt", "en"]

ROTULOS: dict[Idioma, dict[str, str]] = {
    "pt": {
        "medido": "Medido: {frases}.",
        "hipotese": "Hipótese (força {forca}): {texto}. Evidência: {evidencia}.",
        "opcoes": "Opções: {opcoes}.",
        "exemplo": "Dados de exemplo: posições aproximadas e valores ilustrativos.",
        "erro": "Não consegui responder agora. Tente de novo em instantes.",
    },
    "en": {
        "medido": "Measured: {frases}.",
        "hipotese": "Hypothesis ({forca} strength): {texto}. Evidence: {evidencia}.",
        "opcoes": "Options: {opcoes}.",
        "exemplo": "Sample data: approximate positions and illustrative values.",
        "erro": "I couldn't answer right now. Please try again in a moment.",
    },
}
FORCAS: dict[Idioma, dict[str, str]] = {
    "pt": {"fraca": "fraca", "moderada": "moderada", "forte": "forte"},
    "en": {"fraca": "weak", "moderada": "moderate", "forte": "strong"},
}


def para_contrato(resposta: RespostaCass, ferramentas: list[str]) -> RespostaAgente:
    """RespostaCass -> RespostaAgente. `ferramentas` é o que o código viu ser usado.

    Levanta ValueError se não sobrar nenhum parágrafo (o contrato exige pelo menos um).
    """
    rotulos = ROTULOS[resposta.idioma]
    paragrafos = [p.strip() for p in re.split(r"\n\s*\n", resposta.texto) if p.strip()]
    if not paragrafos:
        raise ValueError("a RespostaCass veio sem texto")

    frases = [_sem_ponto(m) for m in resposta.medido if m.strip()]
    if frases:
        paragrafos.append(rotulos["medido"].format(frases="; ".join(frases)))
    if resposta.hipotese:
        paragrafos.append(
            rotulos["hipotese"].format(
                forca=FORCAS[resposta.idioma][resposta.hipotese.forca],
                texto=_sem_ponto(resposta.hipotese.texto),
                evidencia=_sem_ponto(resposta.hipotese.evidencia),
            )
        )
    if resposta.opcoes:
        paragrafos.append(rotulos["opcoes"].format(opcoes=", ".join(resposta.opcoes)))

    fontes = [f"{f.nome}, {f.data.strip()}" if f.data.strip() else f.nome for f in resposta.fontes]
    return RespostaAgente(
        paragraphs=paragrafos,
        tools=ferramentas,
        sources=list(dict.fromkeys(fontes)),
        note=rotulos["exemplo"] if resposta.dados_de_exemplo else None,
    )


def resposta_de_erro(idioma: Idioma) -> RespostaAgente:
    """Corpo das respostas 500 e 504, no formato do contrato."""
    return RespostaAgente(paragraphs=[ROTULOS[idioma]["erro"]], tools=[], sources=[])


def _sem_ponto(frase: str) -> str:
    return frase.strip().rstrip(".").strip()
