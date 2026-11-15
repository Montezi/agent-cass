"""Testes das 8 ferramentas, com o respx servindo os dados (sem chamar o Gemini)."""

import json
import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from cass import ferramentas
from cass.ferramentas import (
    consultar_hotspot,
    contexto_enso,
    explicar_classe,
    exposicao,
    hipotese,
    listar_hotspots,
    previsao_tempo,
    rascunhar_alerta,
)

FIXTURE = Path(__file__).parent / "fixtures" / "hotspots.geojson"
DADOS_BASE = "http://dados.teste/dados"
URL = f"{DADOS_BASE}/hotspots.geojson"
URL_METEOMATICS = "https://api.meteomatics.com"
URL_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
IDS = ["norte-fire-ca", "costa-peru", "norte-amazonia", "guaiba-rs", "kariba", "borneo"]
SEM_STATS = {"disponivel": False, "motivo": "ainda não calculado pelo pipeline"}
AGORA = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
DIAS = [
    {"date": "2026-11-15", "precipitation_mm": 18},
    {"date": "2026-11-16", "precipitation_mm": 6.5},
    {"date": "2026-11-17", "precipitation_mm": 0},
    {"date": "2026-11-18", "precipitation_mm": 2},
]
URL_SNAPSHOT = f"{DADOS_BASE}/forecast-snapshot.json"
# Formato do forecastSnapshotSchema do frontend.
SNAPSHOT = {
    "source": "snapshot",
    "updated_at": "2026-11-14T09:00:00Z",
    "forecasts": {
        "guaiba-rs": [
            {"date": "2026-11-15", "precipitation_mm": 12},
            {"date": "2026-11-16", "precipitation_mm": 3.5},
            {"date": "2026-11-17", "precipitation_mm": 0},
        ],
    },
}


@pytest.fixture(autouse=True)
def ambiente(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    monkeypatch.setenv("DADOS_BASE", DADOS_BASE)
    monkeypatch.setenv("METEOMATICS_USER", "usuario-teste")
    monkeypatch.setenv("METEOMATICS_PASSWORD", "senha-teste")
    monkeypatch.setenv("APP_ORIGIN", "http://app.teste")
    monkeypatch.setattr(ferramentas, "_agora", lambda: AGORA)
    ferramentas.limpar_cache()
    yield
    ferramentas.limpar_cache()


def fixture_com(**propriedades_do_guaiba: Any) -> dict:
    dados = json.loads(FIXTURE.read_text(encoding="utf-8"))
    dados["features"][3]["properties"].update(propriedades_do_guaiba)
    return dados


@pytest.fixture
def rota(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(URL).mock(
        return_value=httpx.Response(200, content=FIXTURE.read_bytes())
    )


def meteomatics(dias: list[dict] = DIAS, gerada: str = "2026-11-14T09:00:00Z") -> dict:
    """Resposta da Meteomatics: precip_24h às 00:00Z de D+1 é a chuva do dia D."""
    datas = [
        {"date": f"{date.fromisoformat(d['date']) + timedelta(days=1)}T00:00:00Z", "value": d["precipitation_mm"]}
        for d in dias
    ]
    return {
        "version": "3.0", "user": "usuario-teste", "dateGenerated": gerada, "status": "OK",
        "data": [{"parameter": "precip_24h:mm", "coordinates": [{"lat": -30.0, "lon": -51.2, "dates": datas}]}],
    }


@pytest.fixture
def clima(respx_mock: respx.MockRouter) -> respx.Route:
    """A primeira fonte da cadeia (Meteomatics) respondendo."""
    return respx_mock.get(url__startswith=URL_METEOMATICS).mock(
        return_value=httpx.Response(200, json=meteomatics())
    )


@pytest.fixture
def ao_vivo_fora(respx_mock: respx.MockRouter) -> None:
    """Meteomatics e Open-Meteo com HTTP 500: só sobra a previsão salva."""
    respx_mock.get(url__startswith=URL_METEOMATICS).mock(return_value=httpx.Response(500))
    respx_mock.get(url__startswith=URL_OPEN_METEO).mock(return_value=httpx.Response(500))


def ao_vivo_falha(respx_mock: respx.MockRouter, falha: httpx.Response | Exception) -> None:
    for url in (URL_METEOMATICS, URL_OPEN_METEO):
        rota = respx_mock.get(url__startswith=url)
        if isinstance(falha, Exception):
            rota.mock(side_effect=falha)
        else:
            rota.mock(return_value=falha)


@pytest.fixture
def reserva(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(URL_SNAPSHOT).mock(return_value=httpx.Response(200, json=SNAPSHOT))


@pytest.fixture
def sem_reserva(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(URL_SNAPSHOT).mock(return_value=httpx.Response(404))


def assert_e_json(resultado: dict) -> None:
    # O @tool do Strands faz json.dumps do dict; se falhar, manda o repr do Python.
    json.dumps(resultado)


# ---------- especificação que o modelo vê ----------


@pytest.mark.parametrize(
    ("ferramenta", "obrigatorios"),
    [
        (listar_hotspots, []),
        (consultar_hotspot, ["id"]),
        (explicar_classe, ["id", "classe"]),
        (hipotese, ["id"]),
        (exposicao, ["id"]),
        (contexto_enso, ["id"]),
        (previsao_tempo, ["id"]),
        (rascunhar_alerta, ["id"]),
    ],
)
def test_especificacao_da_ferramenta(ferramenta: Any, obrigatorios: list[str]) -> None:
    esquema = ferramenta.tool_spec["inputSchema"]["json"]

    assert ferramenta.tool_spec["description"]
    assert sorted(esquema.get("required", [])) == sorted(obrigatorios)
    for parametro in esquema["properties"].values():
        assert parametro.get("description"), parametro


# ---------- id inválido: mesma validação em todas ----------

COM_ID: list[Callable[[str], dict]] = [
    consultar_hotspot,
    lambda i: explicar_classe(i, "flood"),
    hipotese,
    exposicao,
    contexto_enso,
    previsao_tempo,
    rascunhar_alerta,
]


@pytest.mark.parametrize("chamar", COM_ID)
def test_id_desconhecido_devolve_erro_com_validos(
    rota: respx.Route, clima: respx.Route, chamar: Callable[[str], dict]
) -> None:
    resultado = chamar("lago-guaiba")

    assert "lago-guaiba" in resultado["erro"]
    assert resultado["validos"] == IDS
    assert clima.call_count == 0


@pytest.mark.parametrize("chamar", COM_ID)
def test_timeout_nos_hotspots_vira_erro(
    respx_mock: respx.MockRouter, chamar: Callable[[str], dict]
) -> None:
    respx_mock.get(URL).mock(side_effect=httpx.ReadTimeout("demorou"))

    resultado = chamar("guaiba-rs")

    assert "5 s" in resultado["erro"]
    assert resultado["url"] == URL
    assert_e_json(resultado)


# ---------- listar_hotspots ----------


def test_listar_hotspots(rota: respx.Route) -> None:
    resultado = listar_hotspots()

    assert resultado["fonte"] == {"nome": "NISAR", "data": None, "url": URL}
    assert resultado["dados_de_exemplo"] is True
    assert [h["id"] for h in resultado["hotspots"]] == IDS
    assert resultado["hotspots"][3] == {
        "id": "guaiba-rs",
        "name": {"pt": "Lago Guaíba, RS", "en": "Guaíba Lake, RS (Brazil)"},
        "dominant_class": None,
    }


def test_listar_hotspots_data_da_fonte_e_a_passagem_mais_recente(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(URL).mock(
        return_value=httpx.Response(200, json=fixture_com(last_pass="2026-11-12"))
    )

    assert listar_hotspots()["fonte"]["data"] == "2026-11-12"


# ---------- consultar_hotspot ----------


def test_consultar_hotspot(rota: respx.Route) -> None:
    resultado = consultar_hotspot("guaiba-rs")

    assert resultado["fonte"] == {"nome": "NISAR", "data": None, "url": URL}
    assert resultado["dados_de_exemplo"] is True
    hotspot = resultado["hotspot"]
    assert hotspot["id"] == "guaiba-rs"
    assert hotspot["bbox"] == [-51.35, -30.25, -51.0, -29.85]


def test_consultar_hotspot_nao_traz_o_enso_effect(rota: respx.Route) -> None:
    # O enso_effect é contexto da NOAA CPC: só vem por contexto_enso, com essa fonte.
    assert "enso_effect" not in consultar_hotspot("guaiba-rs")["hotspot"]


def test_consultar_hotspot_mantem_os_nulls(rota: respx.Route) -> None:
    # null diz ao modelo "ainda não medido"; sumir com a chave convidaria a inventar.
    hotspot = consultar_hotspot("guaiba-rs")["hotspot"]

    for campo in ("dominant_class", "affected_area_km2", "confidence", "last_pass"):
        assert campo in hotspot
        assert hotspot[campo] is None


def test_consultar_hotspot_data_da_fonte_e_o_last_pass(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(
        return_value=httpx.Response(200, json=fixture_com(last_pass="2026-11-12"))
    )

    resultado = consultar_hotspot("guaiba-rs")

    assert resultado["fonte"]["data"] == "2026-11-12"
    assert resultado["hotspot"]["last_pass"] == "2026-11-12"
    assert_e_json(resultado)


# ---------- explicar_classe, hipotese, exposicao: sem stats ainda ----------


@pytest.mark.parametrize(
    "chamar",
    [lambda: explicar_classe("guaiba-rs", "flood"), lambda: hipotese("guaiba-rs"), lambda: exposicao("guaiba-rs")],
)
def test_sem_stats_nao_inventa_nada(rota: respx.Route, chamar: Callable[[], dict]) -> None:
    assert chamar() == SEM_STATS


def test_explicar_classe_recusa_classe_desconhecida(rota: respx.Route) -> None:
    resultado = explicar_classe("guaiba-rs", "enchente")

    assert "enchente" in resultado["erro"]
    assert resultado["validos"] == ["flood", "drought", "fire_deforestation", "deformation", "stable"]


# ---------- contexto_enso ----------


def test_contexto_enso(rota: respx.Route) -> None:
    resultado = contexto_enso("guaiba-rs")

    assert resultado["hotspot_id"] == "guaiba-rs"
    assert resultado["enso_effect"] == {
        "pt": "chuva acima da média",
        "en": "above-average rainfall",
    }
    assert resultado["dados_de_exemplo"] is True
    assert resultado["fonte"] == {"nome": "NOAA CPC (contexto regional)", "data": None, "url": URL}


def test_contexto_enso_sem_efeito(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(return_value=httpx.Response(200, json=fixture_com(enso_effect=None)))

    assert contexto_enso("guaiba-rs")["enso_effect"] is None


# ---------- previsao_tempo ----------


def test_previsao_tempo(rota: respx.Route, clima: respx.Route) -> None:
    resultado = previsao_tempo("guaiba-rs", 3)

    assert resultado["fonte"]["nome"] == "Meteomatics"
    assert resultado["fonte"]["data"] == "2026-11-14"
    assert resultado["atualizada_em"] == "2026-11-14T09:00:00Z"
    assert resultado["so_referencia"] is False
    assert resultado["dias"] == [
        {"date": "2026-11-15", "precipitation_mm": 18},
        {"date": "2026-11-16", "precipitation_mm": 6.5},
        {"date": "2026-11-17", "precipitation_mm": 0},
    ]
    assert_e_json(resultado)


def test_previsao_tempo_chama_a_cadeia_direto_sem_a_api_clima(
    rota: respx.Route, clima: respx.Route, respx_mock: respx.MockRouter
) -> None:
    previsao_tempo("guaiba-rs")

    # Só o hotspots.geojson e a Meteomatics: nenhum GET para a /api/clima.
    assert [str(c.request.url) for c in respx_mock.calls] == [URL, str(clima.calls.last.request.url)]
    pedido = clima.calls.last.request
    assert pedido.url.path.endswith("/precip_24h:mm/-30.0,-51.2/json")
    assert pedido.extensions["timeout"]["read"] == 3.0


def test_previsao_tempo_padrao_e_3_dias(rota: respx.Route, clima: respx.Route) -> None:
    assert len(previsao_tempo("guaiba-rs")["dias"]) == 3


@pytest.mark.parametrize("dias", [0, 8, -1])
def test_previsao_tempo_recusa_dias_fora_de_1_a_7(
    rota: respx.Route, clima: respx.Route, dias: int
) -> None:
    resultado = previsao_tempo("guaiba-rs", dias)

    assert "1 a 7" in resultado["erro"]
    assert clima.call_count == 0


def test_previsao_tempo_devolve_7_dias(rota: respx.Route, respx_mock: respx.MockRouter) -> None:
    dias = [{"date": f"2026-11-{15 + i}", "precipitation_mm": i} for i in range(7)]
    respx_mock.get(url__startswith=URL_METEOMATICS).respond(200, json=meteomatics(dias))

    assert previsao_tempo("guaiba-rs", 7)["dias"] == dias


def test_previsao_tempo_repassa_a_fonte_open_meteo(
    rota: respx.Route, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(url__startswith=URL_METEOMATICS).respond(500)
    respx_mock.get(url__startswith=URL_OPEN_METEO).respond(200, json={
        "daily_units": {"time": "iso8601", "precipitation_sum": "mm"},
        "daily": {"time": ["2026-11-15", "2026-11-16"], "precipitation_sum": [4.2, 0.0]},
    })

    resultado = previsao_tempo("guaiba-rs")

    # Sem a hora do modelo, a Open-Meteo vale a partir da consulta (AGORA).
    assert resultado["fonte"]["nome"] == "Open-Meteo"
    assert resultado["atualizada_em"] == "2026-11-14T12:00:00Z"
    assert resultado["dias"] == [
        {"date": "2026-11-15", "precipitation_mm": 4.2},
        {"date": "2026-11-16", "precipitation_mm": 0.0},
    ]
    assert "reserva" not in resultado


def test_previsao_antiga_e_so_referencia(rota: respx.Route, respx_mock: respx.MockRouter) -> None:
    # api-contract.md: mais de 3 dias, só referência; nunca dispara aviso de chuva.
    respx_mock.get(url__startswith=URL_METEOMATICS).respond(
        200, json=meteomatics(gerada="2026-11-10T09:00:00Z")
    )

    assert previsao_tempo("guaiba-rs")["so_referencia"] is True


@pytest.mark.parametrize(
    "falha",
    [
        pytest.param(httpx.ReadTimeout("demorou"), id="timeout"),
        pytest.param(httpx.Response(500), id="http-500"),
        pytest.param(httpx.Response(200, json={"status": "ERROR"}), id="fora-do-formato"),
    ],
)
def test_todas_as_fontes_fora_vira_erro(
    rota: respx.Route,
    respx_mock: respx.MockRouter,
    sem_reserva: respx.Route,
    falha: httpx.Response | Exception,
) -> None:
    ao_vivo_falha(respx_mock, falha)

    resultado = previsao_tempo("guaiba-rs")

    erro = resultado["erro"]
    assert erro.startswith("nenhuma fonte de previsão serviu: ")
    assert "a Meteomatics" in erro and "a Open-Meteo" in erro and "o forecast-snapshot.json" in erro
    assert "fonte" not in resultado
    assert sem_reserva.call_count == 1
    assert_e_json(resultado)


def test_timeout_nas_fontes_diz_o_tempo(
    rota: respx.Route, respx_mock: respx.MockRouter, sem_reserva: respx.Route
) -> None:
    ao_vivo_falha(respx_mock, httpx.ConnectTimeout("demorou"))

    erro = previsao_tempo("guaiba-rs")["erro"]
    assert "a Meteomatics não respondeu em 3 s" in erro
    assert "a Open-Meteo não respondeu em 3 s" in erro


# ---------- previsão salva (forecast-snapshot.json), o último degrau ----------


@pytest.mark.parametrize(
    "falha",
    [
        pytest.param(httpx.ReadTimeout("demorou"), id="timeout"),
        pytest.param(httpx.ConnectError("recusou"), id="sem-conexao"),
        pytest.param(httpx.Response(503), id="http-503"),
        pytest.param(httpx.Response(200, text="<html>"), id="nao-e-json"),
        pytest.param(httpx.Response(200, json={"status": "OK"}), id="fora-do-formato"),
    ],
)
def test_falha_ao_vivo_usa_a_previsao_salva(
    rota: respx.Route,
    respx_mock: respx.MockRouter,
    reserva: respx.Route,
    falha: httpx.Response | Exception,
) -> None:
    ao_vivo_falha(respx_mock, falha)

    resultado = previsao_tempo("guaiba-rs", 2)

    assert resultado["fonte"] == {"nome": "Snapshot", "data": "2026-11-14", "url": URL_SNAPSHOT}
    assert resultado["reserva"] is True
    assert "a Meteomatics" in resultado["motivo_da_reserva"]
    assert "a Open-Meteo" in resultado["motivo_da_reserva"]
    assert resultado["atualizada_em"] == "2026-11-14T09:00:00Z"
    assert resultado["dias"] == [
        {"date": "2026-11-15", "precipitation_mm": 12},
        {"date": "2026-11-16", "precipitation_mm": 3.5},
    ]
    assert_e_json(resultado)


def test_previsao_ao_vivo_nao_le_a_salva(
    rota: respx.Route, clima: respx.Route, reserva: respx.Route, respx_mock: respx.MockRouter
) -> None:
    open_meteo = respx_mock.get(url__startswith=URL_OPEN_METEO)

    resultado = previsao_tempo("guaiba-rs")

    assert resultado["fonte"]["nome"] == "Meteomatics"
    assert "reserva" not in resultado
    assert open_meteo.call_count == reserva.call_count == 0


def test_previsao_salva_de_exemplo_marca_dados_de_exemplo(
    rota: respx.Route, respx_mock: respx.MockRouter, ao_vivo_fora: None
) -> None:
    respx_mock.get(URL_SNAPSHOT).mock(
        return_value=httpx.Response(200, json={**SNAPSHOT, "metadata": {"sample": True}})
    )

    assert previsao_tempo("guaiba-rs")["dados_de_exemplo"] is True


def test_previsao_salva_antiga_e_so_referencia(
    rota: respx.Route, respx_mock: respx.MockRouter, ao_vivo_fora: None
) -> None:
    respx_mock.get(URL_SNAPSHOT).mock(
        return_value=httpx.Response(200, json={**SNAPSHOT, "updated_at": "2026-11-10T09:00:00Z"})
    )

    resultado = previsao_tempo("guaiba-rs")

    assert resultado["so_referencia"] is True
    assert resultado["fonte"]["data"] == "2026-11-10"


def test_previsao_salva_sem_o_hotspot_vira_erro(
    rota: respx.Route, respx_mock: respx.MockRouter, ao_vivo_fora: None
) -> None:
    respx_mock.get(URL_SNAPSHOT).mock(
        return_value=httpx.Response(200, json={**SNAPSHOT, "forecasts": {"kariba": SNAPSHOT["forecasts"]["guaiba-rs"]}})
    )

    resultado = previsao_tempo("guaiba-rs")

    assert "não tem o hotspot 'guaiba-rs'" in resultado["erro"]
    assert "HTTP 500" in resultado["erro"]


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param({**SNAPSHOT, "source": "meteomatics"}, id="fonte-errada"),
        pytest.param({**SNAPSHOT, "forecasts": {"guaiba-rs": []}}, id="lista-vazia"),
        pytest.param({k: v for k, v in SNAPSHOT.items() if k != "updated_at"}, id="sem-data"),
    ],
)
def test_previsao_salva_fora_do_contrato_vira_erro(
    rota: respx.Route, respx_mock: respx.MockRouter, ao_vivo_fora: None, snapshot: dict
) -> None:
    respx_mock.get(URL_SNAPSHOT).mock(return_value=httpx.Response(200, json=snapshot))

    assert "não segue o contrato" in previsao_tempo("guaiba-rs")["erro"]


def test_alerta_usa_a_previsao_salva(
    rota: respx.Route, ao_vivo_fora: None, reserva: respx.Route
) -> None:
    resultado = rascunhar_alerta("guaiba-rs")

    assert "12 mm em 15 nov" in resultado["rascunho"]
    assert [f["nome"] for f in resultado["fontes"]] == ["NISAR", "Snapshot"]


def test_previsao_tempo_usa_cache(rota: respx.Route, clima: respx.Route) -> None:
    previsao_tempo("guaiba-rs")
    previsao_tempo("guaiba-rs", 7)

    assert clima.call_count == 1


# ---------- rascunhar_alerta ----------


def numeros(texto: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", texto))


def test_alerta_sem_medicao(rota: respx.Route, clima: respx.Route) -> None:
    resultado = rascunhar_alerta("guaiba-rs")

    texto = resultado["rascunho"]
    assert resultado["enviado"] is False
    assert texto.startswith("Alerta CassAAndra: Lago Guaíba, RS")
    assert "o radar ainda não tem medição para esta área" in texto
    assert "18 mm em 15 nov" in texto
    assert "6,5 mm em 16 nov" in texto
    assert "Dados de exemplo" in texto
    assert texto.endswith("Veja no app: http://app.teste/?hotspot=guaiba-rs")
    assert resultado["dados_de_exemplo"] is True
    assert resultado["fontes"] == [
        {"nome": "NISAR", "data": None, "url": URL},
        {"nome": "Meteomatics", "data": "2026-11-14", "url": clima.calls.last.request.url.__str__()},
    ]
    assert_e_json(resultado)


def test_alerta_com_medicao_em_portugues(respx_mock: respx.MockRouter, clima: respx.Route) -> None:
    respx_mock.get(URL).mock(
        return_value=httpx.Response(
            200,
            json=fixture_com(
                dominant_class="flood",
                affected_area_km2=212.5,
                confidence=0.86,
                last_pass="2026-11-12",
            ),
        )
    )

    texto = rascunhar_alerta("guaiba-rs", "pt")["rascunho"]

    assert "Medido: Cheia em 212,5 km² (confiança de 86%), passagem do radar de 12 nov 2026." in texto


def test_alerta_em_ingles(respx_mock: respx.MockRouter, clima: respx.Route) -> None:
    respx_mock.get(URL).mock(
        return_value=httpx.Response(
            200,
            json=fixture_com(
                dominant_class="flood",
                affected_area_km2=212.5,
                confidence=0.86,
                last_pass="2026-11-12",
            ),
        )
    )

    texto = rascunhar_alerta("guaiba-rs", "en")["rascunho"]

    assert texto.startswith("CassAAndra alert: Guaíba Lake, RS (Brazil)")
    assert "Measured: Flood over 212.5 km² (86% confidence), radar pass of Nov 12, 2026." in texto
    assert "18 mm on Nov 15" in texto
    assert "Sample data" in texto


def test_alerta_nao_inventa_numeros(respx_mock: respx.MockRouter, clima: respx.Route) -> None:
    dados = fixture_com(
        dominant_class="flood", affected_area_km2=212.5, confidence=0.86, last_pass="2026-11-12"
    )
    respx_mock.get(URL).mock(return_value=httpx.Response(200, json=dados))

    texto = rascunhar_alerta("guaiba-rs")["rascunho"]

    # Todo número vem dos dados: área, confiança (86%), datas (passagem, previsão e
    # geração da previsão) e chuva por dia.
    permitidos = {"212,5", "86", "12", "2026", "14", "15", "16", "17", "18", "6,5", "0"}
    assert numeros(texto) <= permitidos, numeros(texto) - permitidos


def test_alerta_sem_previsao_ainda_sai(
    rota: respx.Route, respx_mock: respx.MockRouter, sem_reserva: respx.Route
) -> None:
    ao_vivo_falha(respx_mock, httpx.ConnectTimeout("demorou"))

    resultado = rascunhar_alerta("guaiba-rs")

    assert "Previsão: indisponível agora." in resultado["rascunho"]
    assert [f["nome"] for f in resultado["fontes"]] == ["NISAR"]


def test_alerta_com_previsao_antiga_avisa(rota: respx.Route, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__startswith=URL_METEOMATICS).respond(
        200, json=meteomatics(gerada="2026-11-10T09:00:00Z")
    )

    texto = rascunhar_alerta("guaiba-rs")["rascunho"]

    assert "Previsão de 10 nov 2026, só para referência" in texto
    assert "18 mm" not in texto


def test_alerta_nao_inclui_hipotese_nem_exposicao_sem_dados(
    rota: respx.Route, clima: respx.Route
) -> None:
    texto = rascunhar_alerta("guaiba-rs")["rascunho"]

    assert "Hipótese" not in texto
    assert "El Niño" not in texto


def test_alerta_recusa_idioma_desconhecido(rota: respx.Route, clima: respx.Route) -> None:
    resultado = rascunhar_alerta("guaiba-rs", "es")

    assert resultado["validos"] == ["pt", "en"]
    assert clima.call_count == 0


# ---------- leitura e cache dos hotspots ----------


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
    hipotese("guaiba-rs")
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
        assert resultado["url"] == URL
        assert "fonte" not in resultado
        assert_e_json(resultado)


def test_erro_nao_fica_no_cache(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, content=FIXTURE.read_bytes()),
        ]
    )

    assert "erro" in listar_hotspots()
    assert len(listar_hotspots()["hotspots"]) == 6
