"""Testes das ferramentas, com o respx servindo o fixture (sem chamar o Gemini)."""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from cass import ferramentas
from cass.ferramentas import consultar_hotspot, listar_hotspots

FIXTURE = Path(__file__).parent / "fixtures" / "hotspots.geojson"
DADOS_BASE = "http://dados.teste/dados"
URL = f"{DADOS_BASE}/hotspots.geojson"
IDS = ["norte-fire-ca", "costa-peru", "norte-amazonia", "guaiba-rs", "kariba", "borneo"]


@pytest.fixture(autouse=True)
def ambiente(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    monkeypatch.setenv("DADOS_BASE", DADOS_BASE)
    ferramentas.limpar_cache()
    yield
    ferramentas.limpar_cache()


@pytest.fixture
def rota(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(URL).mock(
        return_value=httpx.Response(200, content=FIXTURE.read_bytes())
    )


# ---------- especificação das ferramentas ----------


def test_ferramentas_tem_nome_e_parametros_para_o_modelo() -> None:
    assert listar_hotspots.tool_name == "listar_hotspots"
    assert consultar_hotspot.tool_name == "consultar_hotspot"

    esquema = consultar_hotspot.tool_spec["inputSchema"]["json"]
    assert esquema["required"] == ["id"]
    assert esquema["properties"]["id"]["type"] == "string"


# ---------- listar_hotspots ----------


def test_listar_hotspots(rota: respx.Route) -> None:
    resultado = listar_hotspots()

    assert resultado["fonte"] == URL
    assert resultado["dados_de_exemplo"] is True
    assert [h["id"] for h in resultado["hotspots"]] == IDS
    guaiba = resultado["hotspots"][3]
    assert guaiba == {
        "id": "guaiba-rs",
        "name": {"pt": "Lago Guaíba, RS", "en": "Guaíba Lake, RS (Brazil)"},
        "dominant_class": None,
    }


# ---------- consultar_hotspot ----------


def test_consultar_hotspot(rota: respx.Route) -> None:
    resultado = consultar_hotspot("guaiba-rs")

    assert resultado["fonte"] == URL
    assert resultado["dados_de_exemplo"] is True
    hotspot = resultado["hotspot"]
    assert hotspot["id"] == "guaiba-rs"
    assert hotspot["enso_effect"]["pt"] == "chuva acima da média"
    assert hotspot["bbox"] == [-51.35, -30.25, -51.0, -29.85]


def test_consultar_hotspot_mantem_os_nulls(rota: respx.Route) -> None:
    # null diz ao modelo "ainda não medido"; sumir com a chave convidaria a inventar.
    hotspot = consultar_hotspot("guaiba-rs")["hotspot"]

    for campo in ("dominant_class", "affected_area_km2", "confidence", "last_pass"):
        assert campo in hotspot
        assert hotspot[campo] is None


def test_consultar_hotspot_desconhecido_devolve_erro_com_validos(rota: respx.Route) -> None:
    resultado = consultar_hotspot("lago-guaiba")

    assert "lago-guaiba" in resultado["erro"]
    assert resultado["validos"] == IDS


# ---------- o que vai para o modelo ----------


def test_resultados_sao_json(rota: respx.Route) -> None:
    # O @tool do Strands faz json.dumps do dict; se falhar, manda o repr do Python.
    for resultado in (listar_hotspots(), consultar_hotspot("guaiba-rs")):
        json.dumps(resultado)


def test_last_pass_vai_como_texto(respx_mock: respx.MockRouter) -> None:
    dados = json.loads(FIXTURE.read_text(encoding="utf-8"))
    dados["features"][3]["properties"]["last_pass"] = "2026-11-12"
    respx_mock.get(URL).mock(return_value=httpx.Response(200, json=dados))

    assert consultar_hotspot("guaiba-rs")["hotspot"]["last_pass"] == "2026-11-12"


# ---------- leitura e cache ----------


def test_le_com_timeout_de_5_segundos(rota: respx.Route) -> None:
    listar_hotspots()

    assert rota.calls.last.request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }


def test_usa_o_cache_entre_chamadas(rota: respx.Route) -> None:
    listar_hotspots()
    consultar_hotspot("guaiba-rs")
    consultar_hotspot("nao-existe")

    assert rota.call_count == 1


def test_cache_expira(rota: respx.Route, monkeypatch: pytest.MonkeyPatch) -> None:
    agora = 1000.0
    monkeypatch.setattr(ferramentas, "monotonic", lambda: agora)
    listar_hotspots()

    agora += ferramentas.CACHE_TTL_S + 1
    listar_hotspots()

    assert rota.call_count == 2


def test_cache_muda_com_dados_base(
    rota: respx.Route, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    outra = respx_mock.get("http://outro.teste/dados/hotspots.geojson").mock(
        return_value=httpx.Response(200, content=FIXTURE.read_bytes())
    )
    listar_hotspots()

    monkeypatch.setenv("DADOS_BASE", "http://outro.teste/dados/")
    listar_hotspots()

    assert (rota.call_count, outra.call_count) == (1, 1)


# ---------- falhas viram dado, não exceção ----------


@pytest.mark.parametrize(
    "resposta",
    [
        pytest.param(httpx.Response(500), id="http-500"),
        pytest.param(httpx.Response(404), id="http-404"),
        pytest.param(httpx.ConnectTimeout("demorou"), id="timeout"),
        pytest.param(httpx.ConnectError("recusou"), id="sem-conexao"),
        pytest.param(httpx.Response(200, text="<html>"), id="nao-e-json"),
        pytest.param(
            httpx.Response(200, json={"type": "FeatureCollection", "features": [{}]}),
            id="fora-do-contrato",
        ),
    ],
)
def test_falha_na_leitura_vira_erro(
    respx_mock: respx.MockRouter, resposta: httpx.Response | Exception
) -> None:
    if isinstance(resposta, Exception):
        respx_mock.get(URL).mock(side_effect=resposta)
    else:
        respx_mock.get(URL).mock(return_value=resposta)

    for resultado in (listar_hotspots(), consultar_hotspot("guaiba-rs")):
        assert resultado["erro"]
        assert resultado["fonte"] == URL
        json.dumps(resultado)


def test_erro_nao_fica_no_cache(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, content=FIXTURE.read_bytes()),
        ]
    )

    assert "erro" in listar_hotspots()
    assert len(listar_hotspots()["hotspots"]) == 6
