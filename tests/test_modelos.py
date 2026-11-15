"""Testes do espelho Pydantic do contrato de dados (docs/data-contract.md)."""

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cass.modelos import ColecaoHotspots

FIXTURE = Path(__file__).parent / "fixtures" / "hotspots.geojson"
CONTRATO = Path(__file__).parent.parent / "docs" / "data-contract.md"
IDS_DO_PROJETO = [
    "norte-fire-ca",
    "costa-peru",
    "norte-amazonia",
    "guaiba-rs",
    "kariba",
    "borneo",
]
MEDIDOS = ("dominant_class", "affected_area_km2", "confidence", "last_pass")

# Os testes de regra violada estragam o Guaíba:
# ponto [-51.2, -30.0], bbox [-51.35, -30.25, -51.0, -29.85].
GUAIBA = 3
FEATURE = ("features", GUAIBA)
PROPS = (*FEATURE, "properties")


@pytest.fixture
def dados() -> dict[str, Any]:
    """Cópia nova do fixture válido em cada teste."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def props(dados: dict[str, Any]) -> dict[str, Any]:
    return dados["features"][GUAIBA]["properties"]


def assert_rejeita(dados: dict[str, Any], loc: tuple, trecho: str = "") -> None:
    """Exige um erro de validação em `loc` (e com `trecho` na mensagem, se dado)."""
    with pytest.raises(ValidationError) as exc:
        ColecaoHotspots.model_validate(dados)
    erros = [(e["loc"], e["msg"]) for e in exc.value.errors()]
    assert any(l[: len(loc)] == loc and trecho in m for l, m in erros), erros


# ---------- arquivos válidos ----------


def test_fixture_e_valido(dados: dict[str, Any]) -> None:
    colecao = ColecaoHotspots.model_validate(dados)

    assert colecao.metadata.sample is True
    assert [h.properties.id for h in colecao.features] == IDS_DO_PROJETO
    for hotspot in colecao.features:
        p = hotspot.properties
        assert p.name.pt and p.name.en
        assert [getattr(p, campo) for campo in MEDIDOS] == [None] * 4


def test_exemplo_do_contrato_e_valido() -> None:
    # O primeiro bloco JSON do docs/data-contract.md (cópia do contrato do frontend).
    bloco = re.search(r"```json\n(.*?)\n```", CONTRATO.read_text(encoding="utf-8"), re.DOTALL)
    assert bloco is not None
    exemplo = json.loads(bloco[1])

    p = ColecaoHotspots.model_validate(exemplo).features[0].properties

    assert p.id == "guaiba-rs"
    assert p.dominant_class == "flood"
    assert p.affected_area_km2 == 212
    assert p.confidence == 0.86
    assert p.last_pass == date(2026, 11, 12)
    assert p.baseline_pass == date(2026, 1, 14)
    assert p.threshold_db == 3
    assert p.layers == {"flood": "layers/guaiba-rs/flood.png"}


def test_campos_extras_sao_permitidos_e_ignorados(dados: dict[str, Any]) -> None:
    dados["gerado_por"] = "pipeline"
    props(dados)["delta_hh_db"] = -3.2
    props(dados)["name"]["es"] = "Lago Guaíba"

    p = ColecaoHotspots.model_validate(dados).features[GUAIBA].properties

    assert "delta_hh_db" not in p.model_dump()
    assert "es" not in p.name.model_dump()


def test_metadata_e_opcional(dados: dict[str, Any]) -> None:
    del dados["metadata"]

    assert ColecaoHotspots.model_validate(dados).metadata.sample is False


def test_sample_e_opcional_dentro_de_metadata(dados: dict[str, Any]) -> None:
    dados["metadata"] = {}

    assert ColecaoHotspots.model_validate(dados).metadata.sample is False


def test_short_name_e_opcional(dados: dict[str, Any]) -> None:
    del props(dados)["short_name"]

    colecao = ColecaoHotspots.model_validate(dados)

    assert colecao.features[GUAIBA].properties.short_name is None


def test_enso_effect_aceita_null(dados: dict[str, Any]) -> None:
    props(dados)["enso_effect"] = None

    colecao = ColecaoHotspots.model_validate(dados)

    assert colecao.features[GUAIBA].properties.enso_effect is None


def test_inteiro_vale_como_numero(dados: dict[str, Any]) -> None:
    props(dados)["affected_area_km2"] = 88
    props(dados)["confidence"] = 1

    p = ColecaoHotspots.model_validate(dados).features[GUAIBA].properties

    assert (p.affected_area_km2, p.confidence) == (88, 1)


def test_ponto_na_borda_do_bbox_e_aceito(dados: dict[str, Any]) -> None:
    dados["features"][GUAIBA]["geometry"]["coordinates"] = [-51.35, -29.85]

    ColecaoHotspots.model_validate(dados)


def test_extensao_png_em_maiusculas_e_aceita(dados: dict[str, Any]) -> None:
    # O frontend usa /\.png$/i.
    props(dados)["layers"] = {"flood": "layers/guaiba-rs/FLOOD.PNG"}

    ColecaoHotspots.model_validate(dados)


# ---------- id ----------


@pytest.mark.parametrize(
    "valor",
    ["Guaiba-RS", "guaiba_rs", "guaiba--rs", "-guaiba", "guaiba-", "guaíba", "guaiba rs", ""],
)
def test_id_fora_do_padrao(dados: dict[str, Any], valor: str) -> None:
    props(dados)["id"] = valor

    assert_rejeita(dados, (*PROPS, "id"))


def test_id_repetido(dados: dict[str, Any]) -> None:
    props(dados)["id"] = "kariba"

    assert_rejeita(dados, (), "kariba")


# ---------- valores medidos ----------


def test_dominant_class_fora_das_classes(dados: dict[str, Any]) -> None:
    props(dados)["dominant_class"] = "enchente"

    assert_rejeita(dados, (*PROPS, "dominant_class"))


@pytest.mark.parametrize("valor", [-0.01, 1.01, "0.87", True])
def test_confidence_invalida(dados: dict[str, Any], valor: object) -> None:
    props(dados)["confidence"] = valor

    assert_rejeita(dados, (*PROPS, "confidence"))


@pytest.mark.parametrize("valor", [-1, -0.5, "412.5"])
def test_affected_area_invalida(dados: dict[str, Any], valor: object) -> None:
    props(dados)["affected_area_km2"] = valor

    assert_rejeita(dados, (*PROPS, "affected_area_km2"))


@pytest.mark.parametrize(
    "valor",
    ["12/11/2026", "2026-11-12T00:00:00", "20261112", "2026-2-3", "2026-02-30", 20261112],
)
def test_last_pass_fora_do_formato(dados: dict[str, Any], valor: object) -> None:
    props(dados)["last_pass"] = valor

    assert_rejeita(dados, (*PROPS, "last_pass"))


@pytest.mark.parametrize("campo", [*MEDIDOS, "enso_effect"])
def test_campo_anulavel_ainda_e_obrigatorio(dados: dict[str, Any], campo: str) -> None:
    # Como no zod (.nullable() sem .optional()): o valor pode ser null, a chave não pode faltar.
    del props(dados)[campo]

    assert_rejeita(dados, (*PROPS, campo))


# ---------- baseline_pass e threshold_db (opcionais) ----------


def test_baseline_pass_e_threshold_db_sao_opcionais(dados: dict[str, Any]) -> None:
    p = ColecaoHotspots.model_validate(dados).features[GUAIBA].properties

    assert p.baseline_pass is None and p.threshold_db is None


def test_baseline_pass_e_threshold_db_sao_lidos(dados: dict[str, Any]) -> None:
    props(dados).update(baseline_pass="2026-01-14", threshold_db=3)

    p = ColecaoHotspots.model_validate(dados).features[GUAIBA].properties

    assert p.baseline_pass == date(2026, 1, 14)
    assert p.threshold_db == 3


def test_baseline_pass_e_threshold_db_aceitam_null(dados: dict[str, Any]) -> None:
    props(dados).update(baseline_pass=None, threshold_db=None)

    p = ColecaoHotspots.model_validate(dados).features[GUAIBA].properties

    assert p.baseline_pass is None and p.threshold_db is None


@pytest.mark.parametrize("valor", ["14/01/2026", "2026-01-14T00:00:00Z", "2026-1-14", "2026-02-30", 20260114])
def test_baseline_pass_fora_do_formato(dados: dict[str, Any], valor: object) -> None:
    props(dados)["baseline_pass"] = valor

    assert_rejeita(dados, (*PROPS, "baseline_pass"))


@pytest.mark.parametrize("valor", [0, -3, "3", True, float("nan"), float("inf")])
def test_threshold_db_precisa_ser_numero_positivo(dados: dict[str, Any], valor: object) -> None:
    props(dados)["threshold_db"] = valor

    assert_rejeita(dados, (*PROPS, "threshold_db"))


# ---------- nome ----------


def test_nome_sem_ingles(dados: dict[str, Any]) -> None:
    props(dados)["name"] = {"pt": "Lago Guaíba"}

    assert_rejeita(dados, (*PROPS, "name", "en"))


def test_nome_vazio(dados: dict[str, Any]) -> None:
    props(dados)["name"]["pt"] = ""

    assert_rejeita(dados, (*PROPS, "name", "pt"))


# ---------- bbox e ponto ----------


@pytest.mark.parametrize(
    "bbox",
    [
        pytest.param([-51.0, -30.25, -51.35, -29.85], id="west>east"),
        pytest.param([-51.2, -30.25, -51.2, -29.85], id="west=east"),
        pytest.param([-51.35, -29.85, -51.0, -30.25], id="south>north"),
        pytest.param([-51.35, -30.25, -51.0], id="tres-itens"),
        pytest.param([-181, -30.25, -51.0, -29.85], id="longitude-fora"),
        pytest.param([-51.35, -91, -51.0, -29.85], id="latitude-fora"),
    ],
)
def test_bbox_invalido(dados: dict[str, Any], bbox: list[float]) -> None:
    props(dados)["bbox"] = bbox

    assert_rejeita(dados, (*PROPS, "bbox"))


@pytest.mark.parametrize(
    "ponto",
    [
        pytest.param([-50.0, -30.0], id="a-leste"),
        pytest.param([-52.0, -30.0], id="a-oeste"),
        pytest.param([-51.2, -29.0], id="ao-norte"),
        pytest.param([-51.2, -31.0], id="ao-sul"),
    ],
)
def test_ponto_fora_do_bbox(dados: dict[str, Any], ponto: list[float]) -> None:
    dados["features"][GUAIBA]["geometry"]["coordinates"] = ponto

    assert_rejeita(dados, FEATURE, "fora do bbox")


@pytest.mark.parametrize(
    "coordenadas",
    [
        pytest.param([-181, -30.0], id="longitude-fora"),
        pytest.param([-51.2, 91], id="latitude-fora"),
        pytest.param([-51.2, -30.0, 10], id="com-altitude"),
        pytest.param([-51.2], id="so-longitude"),
    ],
)
def test_coordenadas_invalidas(dados: dict[str, Any], coordenadas: list[float]) -> None:
    dados["features"][GUAIBA]["geometry"]["coordinates"] = coordenadas

    assert_rejeita(dados, (*FEATURE, "geometry", "coordinates"))


# ---------- caminhos das camadas ----------


@pytest.mark.parametrize(
    ("caminho", "trecho"),
    [
        pytest.param("/layers/guaiba-rs/flood.png", "/ inicial", id="barra-inicial"),
        pytest.param("/dados/layers/guaiba-rs/flood.png", "/ inicial", id="com-dados"),
        pytest.param("layers/../../segredo.png", "..", id="ponto-ponto"),
        pytest.param("https://exemplo.org/flood.png", "URL", id="url"),
        pytest.param("layers/guaiba-rs/flood.png?v=a1b2c3d", "query string", id="query"),
        pytest.param("layers/guaiba-rs/flood.png#topo", "#", id="fragmento"),
        pytest.param("layers/guaiba-rs/flood.webp", ".png", id="nao-png"),
        pytest.param("layers/guaiba rs/flood.png", "só letras", id="espaco"),
        pytest.param("layers//flood.png", "só letras", id="barra-dupla"),
        pytest.param("", "só letras", id="vazio"),
    ],
)
def test_caminho_de_camada_invalido(
    dados: dict[str, Any], caminho: str, trecho: str
) -> None:
    props(dados)["layers"] = {"flood": caminho}

    assert_rejeita(dados, (*PROPS, "layers", "flood"), trecho)


# ---------- tipos GeoJSON e metadata ----------


def test_tipo_da_colecao_errado(dados: dict[str, Any]) -> None:
    dados["type"] = "Collection"

    assert_rejeita(dados, ("type",))


def test_tipo_da_feature_errado(dados: dict[str, Any]) -> None:
    dados["features"][GUAIBA]["type"] = "feature"

    assert_rejeita(dados, (*FEATURE, "type"))


def test_geometria_que_nao_e_ponto(dados: dict[str, Any]) -> None:
    dados["features"][GUAIBA]["geometry"]["type"] = "Polygon"

    assert_rejeita(dados, (*FEATURE, "geometry", "type"))


def test_sample_que_nao_e_booleano(dados: dict[str, Any]) -> None:
    dados["metadata"]["sample"] = "true"

    assert_rejeita(dados, ("metadata", "sample"))
