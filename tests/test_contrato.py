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


def test_pergunta_aceita_ate_500_caracteres() -> None:
    # api-contract.md do frontend: question at most 500 characters (MAX_QUESTION_CHARS).
    assert len(PedidoAgente(question="x" * 500, language="pt").question) == 500


@pytest.mark.parametrize(
    "corpo",
    [
        pytest.param({"hotspot_id": "guaiba-rs", "language": "pt"}, id="sem-question"),
        pytest.param({"question": "", "language": "pt"}, id="question-vazia"),
        pytest.param({"question": "x" * 501, "language": "pt"}, id="mais-de-500"),
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


FONTES = [
    {"nome": "NISAR", "data": "2026-11-12", "ferramenta": "consultar_hotspot"},
    {"nome": "NISAR", "data": "2026-11-12", "ferramenta": "listar_hotspots"},
    {"nome": "Meteomatics", "data": "2026-11-14", "ferramenta": "previsao_tempo"},
    {"nome": "NOAA CPC (contexto regional)", "data": "", "ferramenta": "contexto_enso"},
]


@pytest.mark.parametrize(
    ("idioma", "esperado"),
    [
        (
            "pt",
            [
                "NISAR, 12 nov 2026",
                "Meteomatics, 14 nov 2026",
                "NOAA CPC (contexto regional), data não informada",
            ],
        ),
        (
            "en",
            [
                "NISAR, Nov 12, 2026",
                "Meteomatics, Nov 14, 2026",
                "NOAA CPC (contexto regional), date not provided",
            ],
        ),
    ],
)
def test_fontes_com_data_formatada_sem_repeticao(idioma: str, esperado: list[str]) -> None:
    contrato = para_contrato(cass(idioma=idioma, fontes=FONTES), [])

    assert contrato.sources == esperado


def test_data_que_nao_e_iso_passa_como_veio() -> None:
    fonte = {"nome": "JRC", "data": "2025", "ferramenta": "exposicao"}

    assert para_contrato(cass(fontes=[fonte]), []).sources == ["JRC, 2025"]


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


# ---------- limites do contrato (agentAnswerSchema do frontend) ----------

BASE_CONTRATO: dict[str, Any] = {"paragraphs": ["Oi."], "tools": [], "sources": []}


def test_resposta_no_limite_e_aceita() -> None:
    RespostaAgente.model_validate(
        {
            "paragraphs": ["p" * 2000] * 12,
            "tools": [f"f{i:02d}" + "x" * 57 for i in range(10)],
            "sources": ["s" * 200] * 10,
            "note": "n" * 500,
        }
    )


@pytest.mark.parametrize(
    "campos",
    [
        pytest.param({"paragraphs": ["p"] * 13}, id="13-paragrafos"),
        pytest.param({"paragraphs": ["p" * 2001]}, id="paragrafo-2001"),
        # O zod conta como o JavaScript: cada emoji vale 2 (1.001 emojis = 2.002).
        pytest.param({"paragraphs": ["🌧" * 1001]}, id="paragrafo-em-utf16"),
        pytest.param({"tools": [f"f{i}" for i in range(11)]}, id="11-ferramentas"),
        pytest.param({"tools": ["f" * 61]}, id="ferramenta-61"),
        pytest.param({"tools": ["consultar_hotspot", "consultar_hotspot"]}, id="repetida"),
        pytest.param({"sources": [f"s{i}" for i in range(11)]}, id="11-fontes"),
        pytest.param({"sources": ["s" * 201]}, id="fonte-201"),
        pytest.param({"note": "n" * 501}, id="nota-501"),
    ],
)
def test_resposta_fora_do_limite_e_recusada(campos: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RespostaAgente.model_validate({**BASE_CONTRATO, **campos})


def resposta_longa() -> RespostaCass:
    """Tudo acima do limite: 30 parágrafos, medido e hipótese enormes, 50 opções e 25 fontes."""
    return cass(
        texto="\n\n".join(f"Parágrafo {i}." for i in range(1, 31)),
        medido=[f"**{i} km²** de água nova na passagem {i} do radar." for i in range(80)],
        hipotese={"texto": "chuva acima da média " * 150, "evidencia": "série " * 100, "forca": "fraca"},
        opcoes=[f"Hotspot de exemplo número {i}" for i in range(50)],
        fontes=[
            {"nome": f"Fonte {i} " + "com nome muito comprido " * 10, "data": "2026-11-12", "ferramenta": "t"}
            for i in range(25)
        ],
        dados_de_exemplo=True,
    )


FERRAMENTAS_DEMAIS = [
    *(f"ferramenta_{i}" for i in range(12)),
    "ferramenta_0",
    "consultar_hotspot_" + "x" * 80,
]


def test_resposta_longa_ainda_sai_valida() -> None:
    contrato = para_contrato(resposta_longa(), FERRAMENTAS_DEMAIS)

    # Passa de novo pela validação, como o app faria com o JSON.
    RespostaAgente.model_validate_json(contrato.model_dump_json())
    assert len(contrato.paragraphs) == 12
    assert all(len(p) <= 2000 for p in contrato.paragraphs)
    assert len(contrato.tools) == 10 and len(set(contrato.tools)) == 10
    assert all(len(f) <= 60 for f in contrato.tools)
    assert len(contrato.sources) == 10 and all(len(s) <= 200 for s in contrato.sources)


def test_excedente_do_texto_vai_para_o_ultimo_paragrafo_do_texto() -> None:
    paragrafos = para_contrato(resposta_longa(), []).paragraphs

    # 3 extras (Medido, Hipótese, Opções) deixam 9 vagas para o texto.
    assert paragrafos[:8] == [f"Parágrafo {i}." for i in range(1, 9)]
    assert paragrafos[8] == " ".join(f"Parágrafo {i}." for i in range(9, 31))
    assert paragrafos[9].startswith("Medido: **0 km²**")
    assert paragrafos[10].startswith("Hipótese (força fraca): chuva")
    assert paragrafos[11].startswith("Opções: Hotspot de exemplo número 0")


def test_paragrafo_cortado_termina_com_reticencias_sem_negrito_aberto() -> None:
    medido = para_contrato(resposta_longa(), []).paragraphs[9]

    assert len(medido) <= 2000
    assert medido.endswith("…")
    assert medido.count("**") % 2 == 0


def test_ferramentas_sem_repeticao_e_na_ordem() -> None:
    tools = para_contrato(cass(), ["consultar_hotspot", "previsao_tempo", "consultar_hotspot"]).tools

    assert tools == ["consultar_hotspot", "previsao_tempo"]


def test_corte_conta_emojis_como_o_javascript() -> None:
    contrato = para_contrato(cass(medido=["🌧" * 1500]), [])

    medido = contrato.paragraphs[-1]
    assert len(medido.encode("utf-16-le")) // 2 <= 2000
    RespostaAgente.model_validate_json(contrato.model_dump_json())


def test_resposta_curta_nao_muda() -> None:
    contrato = para_contrato(cass(texto="Um.\n\nDois."), ["consultar_hotspot"])

    assert contrato.paragraphs == ["Um.", "Dois."]
    assert contrato.tools == ["consultar_hotspot"]
