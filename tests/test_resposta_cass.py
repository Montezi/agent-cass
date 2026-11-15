"""Testes da RespostaCass, o formato interno do agente (apêndice A do plano)."""

import pytest
from pydantic import ValidationError

from cass.modelos import Fonte, Hipotese, RespostaCass


def resposta_minima(**campos: object) -> dict:
    return {"tipo": "resposta", "idioma": "pt", "texto": "Olá.", "confianca": "alta", **campos}


# ---------- campos iguais ao apêndice A ----------


def test_campos_da_resposta() -> None:
    assert list(Fonte.model_fields) == ["nome", "data", "ferramenta"]
    assert list(Hipotese.model_fields) == ["texto", "evidencia", "forca"]
    assert list(RespostaCass.model_fields) == [
        "tipo",
        "idioma",
        "texto",
        "medido",
        "hipotese",
        "fontes",
        "confianca",
        "opcoes",
        "sugestoes",
        "dados_de_exemplo",
    ]


# ---------- RespostaCass ----------


def test_resposta_minima_usa_os_padroes() -> None:
    resposta = RespostaCass.model_validate(resposta_minima())

    assert resposta.medido == []
    assert resposta.hipotese is None
    assert resposta.fontes == []
    assert resposta.opcoes == []
    assert resposta.sugestoes == []
    assert resposta.dados_de_exemplo is False


def test_resposta_completa() -> None:
    resposta = RespostaCass.model_validate(
        resposta_minima(
            medido=["212 km² de água nova em 12 nov 2026"],
            hipotese={"texto": "chuva", "evidencia": "série", "forca": "moderada"},
            fontes=[{"nome": "NISAR GCOV", "data": "2026-11-12", "ferramenta": "consultar_hotspot"}],
        )
    )

    assert resposta.hipotese is not None and resposta.hipotese.forca == "moderada"
    assert resposta.fontes[0].nome == "NISAR GCOV"


@pytest.mark.parametrize(
    "campos",
    [
        pytest.param({"texto": "x" * 901}, id="texto-901"),
        pytest.param({"tipo": "aviso"}, id="tipo"),
        pytest.param({"idioma": "es"}, id="idioma"),
        pytest.param({"confianca": "média"}, id="confianca-com-acento"),
        pytest.param(
            {"hipotese": {"texto": "t", "evidencia": "e", "forca": "certa"}}, id="forca"
        ),
        pytest.param({"fontes": [{"nome": "NISAR GCOV"}]}, id="fonte-incompleta"),
    ],
)
def test_resposta_invalida(campos: dict) -> None:
    with pytest.raises(ValidationError):
        RespostaCass.model_validate(resposta_minima(**campos))


def test_resposta_aceita_900_caracteres() -> None:
    RespostaCass.model_validate(resposta_minima(texto="x" * 900))
