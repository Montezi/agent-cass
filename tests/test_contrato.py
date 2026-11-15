"""Testes da conversão RespostaCass -> contrato público do POST /api/agente."""

from typing import Any

import pytest
from pydantic import ValidationError

from cass.contrato import para_contrato, resposta_de_erro
from cass.modelos import PedidoAgente, RespostaAgente, RespostaCass


def cass(**campos: Any) -> RespostaCass:
    base: dict[str, Any] = {
        "tipo": "resposta",
        "idioma": "pt",
        "texto": "No Guaíba, o radar viu água nova.",
        "confianca": "alta",
    }
    return RespostaCass.model_validate({**base, **campos})


# ---------- modelos do contrato (iguais ao zod do agentApi.ts) ----------


def test_pedido_do_contrato() -> None:
    pedido = PedidoAgente.model_validate(
        {"question": "O que mudou no Guaíba?", "hotspot_id": "guaiba-rs", "language": "pt"}
    )

    assert (pedido.question, pedido.hotspot_id, pedido.language) == (
        "O que mudou no Guaíba?",
        "guaiba-rs",
        "pt",
    )


def test_pedido_sem_hotspot() -> None:
    assert PedidoAgente(question="oi", language="en").hotspot_id is None


@pytest.mark.parametrize(
    "corpo",
    [
        pytest.param({"hotspot_id": "guaiba-rs", "language": "pt"}, id="sem-question"),
        pytest.param({"question": "", "language": "pt"}, id="question-vazia"),
        pytest.param({"question": "x" * 1001, "language": "pt"}, id="mais-de-1000"),
        pytest.param({"question": "oi"}, id="sem-language"),
        pytest.param({"question": "oi", "language": "es"}, id="language-es"),
    ],
)
def test_pedido_invalido(corpo: dict) -> None:
    with pytest.raises(ValidationError):
        PedidoAgente.model_validate(corpo)


def test_resposta_do_contrato_aceita_note_ausente_ou_null() -> None:
    base = {"paragraphs": ["Oi."], "tools": [], "sources": []}

    assert RespostaAgente.model_validate(base).note is None
    assert RespostaAgente.model_validate({**base, "note": None}).note is None


@pytest.mark.parametrize(
    "campos",
    [
        pytest.param({"paragraphs": []}, id="sem-paragrafos"),
        pytest.param({"paragraphs": [""]}, id="paragrafo-vazio"),
        pytest.param({"tools": None}, id="tools-null"),
    ],
)
def test_resposta_do_contrato_invalida(campos: dict) -> None:
    with pytest.raises(ValidationError):
        RespostaAgente.model_validate(
            {"paragraphs": ["Oi."], "tools": [], "sources": [], **campos}
        )


# ---------- para_contrato ----------


def test_resposta_simples() -> None:
    contrato = para_contrato(cass(), ["consultar_hotspot"])

    assert contrato == RespostaAgente(
        paragraphs=["No Guaíba, o radar viu água nova."],
        tools=["consultar_hotspot"],
        sources=[],
        note=None,
    )


def test_texto_vira_um_paragrafo_por_bloco() -> None:
    contrato = para_contrato(cass(texto="Primeiro.\n\n  Segundo.  \n\n\nTerceiro."), [])

    assert contrato.paragraphs == ["Primeiro.", "Segundo.", "Terceiro."]


def test_medido_vira_paragrafo_separado() -> None:
    contrato = para_contrato(
        cass(medido=["**212 km²** de água nova em 12 nov 2026.", "coerência **0,4**"]), []
    )

    assert contrato.paragraphs[-1] == (
        "Medido: **212 km²** de água nova em 12 nov 2026; coerência **0,4**."
    )


@pytest.mark.parametrize(
    ("idioma", "esperado"),
    [
        ("pt", "Hipótese (força moderada): chuva acima da média. Evidência: série de 2023."),
        ("en", "Hypothesis (moderate strength): chuva acima da média. Evidence: série de 2023."),
    ],
)
def test_hipotese_com_forca_no_idioma(idioma: str, esperado: str) -> None:
    hipotese = {"texto": "chuva acima da média.", "evidencia": "série de 2023", "forca": "moderada"}

    contrato = para_contrato(cass(idioma=idioma, hipotese=hipotese), [])

    assert contrato.paragraphs[-1] == esperado


def test_medido_vem_antes_da_hipotese() -> None:
    contrato = para_contrato(
        cass(
            medido=["**212 km²**"],
            hipotese={"texto": "chuva", "evidencia": "série", "forca": "fraca"},
        ),
        [],
    )

    assert contrato.paragraphs[1].startswith("Medido:")
    assert contrato.paragraphs[2].startswith("Hipótese (força fraca):")


def test_opcoes_viram_paragrafo() -> None:
    contrato = para_contrato(
        cass(
            tipo="pergunta_de_volta",
            texto="Qual lago?",
            opcoes=["Lago Guaíba", "Lago Kariba"],
        ),
        [],
    )

    assert contrato.paragraphs == ["Qual lago?", "Opções: Lago Guaíba, Lago Kariba."]


def test_fontes_viram_texto_com_data_sem_repeticao() -> None:
    fontes = [
        {"nome": "NISAR GCOV", "data": "12 nov 2026", "ferramenta": "consultar_hotspot"},
        {"nome": "NISAR GCOV", "data": "12 nov 2026", "ferramenta": "explicar_classe"},
        {"nome": "hotspots.geojson", "data": " ", "ferramenta": "listar_hotspots"},
    ]

    contrato = para_contrato(cass(fontes=fontes), [])

    assert contrato.sources == ["NISAR GCOV, 12 nov 2026", "hotspots.geojson"]


@pytest.mark.parametrize(
    ("idioma", "trecho"),
    [("pt", "Dados de exemplo"), ("en", "Sample data")],
)
def test_dados_de_exemplo_viram_note(idioma: str, trecho: str) -> None:
    contrato = para_contrato(cass(idioma=idioma, dados_de_exemplo=True), [])

    assert contrato.note is not None and contrato.note.startswith(trecho)


def test_texto_vazio_nao_cabe_no_contrato() -> None:
    with pytest.raises(ValueError):
        para_contrato(cass(texto="  \n\n "), [])


# ---------- resposta_de_erro ----------


@pytest.mark.parametrize(("idioma", "trecho"), [("pt", "Tente de novo"), ("en", "try again")])
def test_resposta_de_erro_no_formato_do_contrato(idioma: str, trecho: str) -> None:
    erro = resposta_de_erro(idioma)  # type: ignore[arg-type]

    assert RespostaAgente.model_validate(erro.model_dump()) == erro
    assert trecho in erro.paragraphs[0]
    assert (erro.tools, erro.sources, erro.note) == ([], [], None)
