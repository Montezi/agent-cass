"""GET /api/clima e a cadeia Meteomatics -> Open-Meteo -> forecast-snapshot.json.

O respx serve cada degrau; nenhum teste sai para a rede.
"""

import base64
import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from cass import ferramentas, main
from cass.ferramentas import previsao_tempo

FIXTURE = Path(__file__).parent / "fixtures" / "hotspots.geojson"
DADOS_BASE = "http://dados.teste/dados"
URL_HOTSPOTS = f"{DADOS_BASE}/hotspots.geojson"
URL_SNAPSHOT = f"{DADOS_BASE}/forecast-snapshot.json"
URL_METEOMATICS = "https://api.meteomatics.com"
URL_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
AGORA = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
GUAIBA = {"hotspot": "guaiba-rs", "lat": "-30.0", "lon": "-51.2"}
USUARIO, SENHA = "usuario-teste", "senha-teste"
DIAS = [
    {"date": "2026-11-15", "precipitation_mm": 18.0},
    {"date": "2026-11-16", "precipitation_mm": 6.5},
    {"date": "2026-11-17", "precipitation_mm": 0.0},
]
SNAPSHOT = {
    "metadata": {"sample": True},
    "source": "snapshot",
    "updated_at": "2026-11-13T09:00:00Z",
    "forecasts": {"guaiba-rs": [{"date": "2026-11-14", "precipitation_mm": 12}]},
}
# z.iso.datetime() do frontend: sem fuso além do "Z".
DATA_HORA_ZOD = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


def meteomatics(
    valores: list[float] = (18, 6.5, 0), gerada: str = "2026-11-14T09:00:00Z", **extra: Any
) -> dict:
    """precip_24h:mm às 00:00Z de 16, 17 e 18 nov: a chuva de 15, 16 e 17 nov."""
    inicio = datetime(2026, 11, 16, tzinfo=UTC)
    datas = [
        {"date": f"{inicio + timedelta(days=i):%Y-%m-%dT%H:%M:%SZ}", "value": v}
        for i, v in enumerate(valores)
    ]
    return {
        "version": "3.0", "user": USUARIO, "dateGenerated": gerada, "status": "OK",
        "data": [{"parameter": "precip_24h:mm", "coordinates": [{"lat": -30.0, "lon": -51.2, "dates": datas}]}],
        **extra,
    }


def open_meteo(chuva: list[float | None] = (18, 6.5, 0), unidade: str = "mm") -> dict:
    return {
        "latitude": -30.0, "longitude": -51.2, "generationtime_ms": 0.1, "timezone": "GMT",
        "daily_units": {"time": "iso8601", "precipitation_sum": unidade},
        "daily": {"time": [f"2026-11-{15 + i}" for i in range(len(chuva))], "precipitation_sum": list(chuva)},
    }


@pytest.fixture(autouse=True)
def ambiente(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    monkeypatch.setenv("DADOS_BASE", DADOS_BASE)
    monkeypatch.setenv("METEOMATICS_USER", USUARIO)
    monkeypatch.setenv("METEOMATICS_PASSWORD", SENHA)
    monkeypatch.setattr(ferramentas, "_agora", lambda: AGORA)
    ferramentas.limpar_cache()
    yield
    ferramentas.limpar_cache()


@pytest.fixture
def cliente() -> Iterator[TestClient]:
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def hotspots(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(URL_HOTSPOTS).respond(200, content=FIXTURE.read_bytes())


@pytest.fixture
def rota_meteomatics(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(url__startswith=URL_METEOMATICS)


@pytest.fixture
def rota_open_meteo(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(url__startswith=URL_OPEN_METEO)


@pytest.fixture
def rota_snapshot(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(URL_SNAPSHOT)


def falhar(rota: respx.Route, falha: httpx.Response | Exception) -> None:
    if isinstance(falha, Exception):
        rota.mock(side_effect=falha)
    else:
        rota.mock(return_value=falha)


def coordenadas(id: str) -> dict[str, str]:
    for h in json.loads(FIXTURE.read_text(encoding="utf-8"))["features"]:
        if h["properties"]["id"] == id:
            lon, lat = h["geometry"]["coordinates"]
            return {"hotspot": id, "lat": str(lat), "lon": str(lon)}
    raise KeyError(id)


# ---------- degrau 1: Meteomatics ----------


def test_meteomatics_responde_no_formato_do_contrato(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route, rota_snapshot: respx.Route,
) -> None:
    rota_meteomatics.respond(200, json=meteomatics())

    resposta = cliente.get("/api/clima", params=GUAIBA)

    assert resposta.status_code == 200
    assert resposta.json() == {"source": "meteomatics", "updated_at": "2026-11-14T09:00:00Z", "days": DIAS}
    assert rota_open_meteo.call_count == rota_snapshot.call_count == 0


def test_meteomatics_pede_7_dias_de_chuva_em_24_h_com_basic_auth(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route
) -> None:
    rota_meteomatics.respond(200, json=meteomatics())

    cliente.get("/api/clima", params=GUAIBA)

    pedido = rota_meteomatics.calls.last.request
    # De 15 a 21 nov (UTC): as somas de 24 h que terminam às 00:00Z de 16 a 22 nov.
    assert str(pedido.url) == (
        f"{URL_METEOMATICS}/2026-11-15T00:00:00Z--2026-11-21T00:00:00Z:P1D"
        "/precip_24h:mm/-30.0,-51.2/json"
    )
    esperado = base64.b64encode(f"{USUARIO}:{SENHA}".encode()).decode()
    assert pedido.headers["authorization"] == f"Basic {esperado}"
    assert SENHA not in str(pedido.url)


def test_meteomatics_corta_em_7_dias(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route
) -> None:
    rota_meteomatics.respond(200, json=meteomatics([1.0] * 17))

    dias = cliente.get("/api/clima", params=GUAIBA).json()["days"]

    assert len(dias) == ferramentas.DIAS_DA_PREVISAO == 7


def test_meteomatics_com_fuso_vira_utc_com_z(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route
) -> None:
    rota_meteomatics.respond(200, json=meteomatics(gerada="2026-11-14T11:00:00.250+02:00"))

    atualizada = cliente.get("/api/clima", params=GUAIBA).json()["updated_at"]

    assert atualizada == "2026-11-14T09:00:00Z"
    assert DATA_HORA_ZOD.fullmatch(atualizada)


@pytest.mark.parametrize(
    "credenciais",
    [
        pytest.param({}, id="sem-nenhuma"),
        pytest.param({"METEOMATICS_USER": USUARIO}, id="so-usuario"),
        pytest.param({"METEOMATICS_PASSWORD": SENHA}, id="so-senha"),
        pytest.param({"METEOMATICS_USER": " ", "METEOMATICS_PASSWORD": " "}, id="em-branco"),
    ],
)
def test_sem_credenciais_comeca_na_open_meteo(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch, hotspots: respx.Route,
    rota_meteomatics: respx.Route, rota_open_meteo: respx.Route, credenciais: dict[str, str],
) -> None:
    monkeypatch.delenv("METEOMATICS_USER")
    monkeypatch.delenv("METEOMATICS_PASSWORD")
    for nome, valor in credenciais.items():
        monkeypatch.setenv(nome, valor)
    rota_open_meteo.respond(200, json=open_meteo())

    resposta = cliente.get("/api/clima", params=GUAIBA)

    assert resposta.json()["source"] == "open-meteo"
    assert rota_meteomatics.call_count == 0


@pytest.mark.parametrize(
    "falha",
    [
        pytest.param(httpx.ConnectTimeout("demorou"), id="timeout"),
        pytest.param(httpx.ConnectError("recusou"), id="sem-conexao"),
        pytest.param(httpx.Response(401), id="credencial-errada"),
        pytest.param(httpx.Response(429), id="cota-esgotada"),
        pytest.param(httpx.Response(500), id="http-500"),
        pytest.param(httpx.Response(200, text="<html>"), id="nao-e-json"),
        pytest.param(httpx.Response(200, json={**meteomatics(), "status": "ERROR"}), id="status-erro"),
        pytest.param(httpx.Response(200, json=meteomatics([-999])), id="valor-invalido"),
        pytest.param(httpx.Response(200, json=meteomatics([])), id="sem-datas"),
        pytest.param(
            httpx.Response(200, json={**meteomatics(), "data": [{"parameter": "t_2m:C", "coordinates": []}]}),
            id="outro-parametro",
        ),
        pytest.param(httpx.Response(302, headers={"Location": "https://outro.teste/"}), id="redirecionamento"),
    ],
)
def test_meteomatics_fora_passa_para_a_open_meteo(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route, falha: httpx.Response | Exception,
) -> None:
    falhar(rota_meteomatics, falha)
    rota_open_meteo.respond(200, json=open_meteo())

    resposta = cliente.get("/api/clima", params=GUAIBA)

    assert resposta.status_code == 200
    assert resposta.json()["source"] == "open-meteo"
    assert rota_meteomatics.call_count == 1


# ---------- degrau 2: Open-Meteo ----------


def test_open_meteo_responde_no_formato_do_contrato(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch, hotspots: respx.Route,
    rota_meteomatics: respx.Route, rota_open_meteo: respx.Route,
) -> None:
    rota_meteomatics.respond(503)
    rota_open_meteo.respond(200, json=open_meteo([4.24, 0, 12]))
    # Microssegundos na hora da consulta não podem chegar ao app.
    monkeypatch.setattr(ferramentas, "_agora", lambda: AGORA.replace(microsecond=123456))

    resposta = cliente.get("/api/clima", params=GUAIBA)

    assert resposta.json() == {
        "source": "open-meteo",
        # Sem a hora do modelo, vale a hora da consulta.
        "updated_at": "2026-11-14T12:00:00Z",
        "days": [
            {"date": "2026-11-15", "precipitation_mm": 4.2},
            {"date": "2026-11-16", "precipitation_mm": 0},
            {"date": "2026-11-17", "precipitation_mm": 12},
        ],
    }
    assert dict(rota_open_meteo.calls.last.request.url.params) == {
        "latitude": "-30.0", "longitude": "-51.2", "daily": "precipitation_sum",
        "forecast_days": "7", "timezone": "GMT",
    }


def test_open_meteo_corta_a_serie_no_primeiro_dia_sem_valor(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route,
) -> None:
    rota_meteomatics.respond(500)
    rota_open_meteo.respond(200, json=open_meteo([3, 1, None, 5]))

    dias = cliente.get("/api/clima", params=GUAIBA).json()["days"]

    assert [d["precipitation_mm"] for d in dias] == [3, 1]


@pytest.mark.parametrize(
    "falha",
    [
        pytest.param(httpx.ReadTimeout("demorou"), id="timeout"),
        pytest.param(httpx.Response(400, json={"error": True, "reason": "x"}), id="http-400"),
        pytest.param(httpx.Response(200, json=open_meteo(unidade="inch")), id="outra-unidade"),
        pytest.param(httpx.Response(200, json=open_meteo([None, 2])), id="primeiro-dia-sem-valor"),
        pytest.param(httpx.Response(200, json=open_meteo([-1])), id="chuva-negativa"),
        pytest.param(
            httpx.Response(200, json={**open_meteo(), "daily": {"time": ["2026-11-15"], "precipitation_sum": [1, 2]}}),
            id="tamanhos-diferentes",
        ),
        pytest.param(
            httpx.Response(200, json={**open_meteo(), "daily": {"time": ["15/11/2026"], "precipitation_sum": [1]}}),
            id="data-fora-do-formato",
        ),
    ],
)
def test_open_meteo_fora_passa_para_o_snapshot(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route, rota_snapshot: respx.Route, falha: httpx.Response | Exception,
) -> None:
    rota_meteomatics.respond(500)
    falhar(rota_open_meteo, falha)
    rota_snapshot.respond(200, json=SNAPSHOT)

    resposta = cliente.get("/api/clima", params=GUAIBA)

    assert resposta.status_code == 200
    assert resposta.json()["source"] == "snapshot"


# ---------- degrau 3: forecast-snapshot.json ----------


def test_snapshot_responde_no_formato_do_contrato(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route, rota_snapshot: respx.Route,
) -> None:
    rota_meteomatics.respond(500)
    rota_open_meteo.respond(500)
    rota_snapshot.respond(200, json=SNAPSHOT)

    resposta = cliente.get("/api/clima", params=GUAIBA)

    # Sem "metadata" nem "forecasts": só as chaves do forecastResponseSchema.
    assert resposta.json() == {
        "source": "snapshot",
        "updated_at": "2026-11-13T09:00:00Z",
        "days": [{"date": "2026-11-14", "precipitation_mm": 12}],
    }


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(httpx.Response(404), id="http-404"),
        pytest.param(httpx.ConnectTimeout("demorou"), id="timeout"),
        pytest.param(
            httpx.Response(200, json={**SNAPSHOT, "forecasts": {"kariba": SNAPSHOT["forecasts"]["guaiba-rs"]}}),
            id="sem-o-hotspot",
        ),
        pytest.param(httpx.Response(200, json={**SNAPSHOT, "source": "meteomatics"}), id="fora-do-contrato"),
    ],
)
def test_todas_as_fontes_fora_da_503(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route, rota_snapshot: respx.Route, snapshot: httpx.Response | Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rota_meteomatics.respond(401)
    rota_open_meteo.respond(500)
    falhar(rota_snapshot, snapshot)

    with caplog.at_level(logging.WARNING, logger="cass.ferramentas"):
        resposta = cliente.get("/api/clima", params=GUAIBA)

    # O app lê o forecast-snapshot.json por conta própria e diz isso.
    assert resposta.status_code == 503
    assert resposta.json() == {"detail": "nenhuma fonte de previsão respondeu"}
    assert "a Meteomatics" in caplog.text and "a Open-Meteo" in caplog.text
    assert "o forecast-snapshot.json" in caplog.text
    assert SENHA not in caplog.text


def test_previsao_indisponivel_traz_os_motivos_na_ordem(
    hotspots: respx.Route, rota_meteomatics: respx.Route, rota_open_meteo: respx.Route,
    rota_snapshot: respx.Route,
) -> None:
    rota_meteomatics.respond(401)
    rota_open_meteo.mock(side_effect=httpx.ReadTimeout("demorou"))
    rota_snapshot.respond(404)
    guaiba = ferramentas.hotspots_por_id()["guaiba-rs"]  # type: ignore[index]

    with pytest.raises(ferramentas.PrevisaoIndisponivel) as falha:
        ferramentas.previsao_do_hotspot(guaiba)

    assert falha.value.falhas == (
        "não consegui ler a Meteomatics (HTTP 401)",
        "a Open-Meteo não respondeu em 3 s",
        "não consegui ler o forecast-snapshot.json (HTTP 404)",
    )


# ---------- timeout: 3 s por fonte ao vivo, 5 s no snapshot ----------


def test_fontes_ao_vivo_tem_3_s_e_o_snapshot_5_s(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route, rota_snapshot: respx.Route,
) -> None:
    rota_meteomatics.mock(side_effect=httpx.ConnectTimeout("demorou"))
    rota_open_meteo.mock(side_effect=httpx.ReadTimeout("demorou"))
    rota_snapshot.respond(200, json=SNAPSHOT)

    cliente.get("/api/clima", params=GUAIBA)

    # 3 + 3 s até o snapshot: cabe nos 8 s que o app espera pela /api/clima.
    for rota, prazo in ((rota_meteomatics, 3.0), (rota_open_meteo, 3.0), (rota_snapshot, 5.0)):
        assert rota.calls.last.request.extensions["timeout"] == {
            "connect": prazo, "read": prazo, "write": prazo, "pool": prazo,
        }


def test_timeout_das_fontes_ao_vivo_aparece_no_motivo(
    hotspots: respx.Route, rota_meteomatics: respx.Route, rota_open_meteo: respx.Route,
    rota_snapshot: respx.Route,
) -> None:
    rota_meteomatics.mock(side_effect=httpx.ReadTimeout("demorou"))
    rota_open_meteo.mock(side_effect=httpx.ConnectTimeout("demorou"))
    rota_snapshot.respond(200, json=SNAPSHOT)

    assert previsao_tempo("guaiba-rs")["motivo_da_reserva"] == (
        "a Meteomatics não respondeu em 3 s; a Open-Meteo não respondeu em 3 s"
    )


# ---------- cache de 30 min por hotspot ----------


@pytest.fixture
def relogio(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    agora = [1000.0]
    monkeypatch.setattr(ferramentas, "monotonic", lambda: agora[0])
    return agora


def test_cache_de_30_min_por_hotspot(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    relogio: list[float],
) -> None:
    rota_meteomatics.respond(200, json=meteomatics())

    cliente.get("/api/clima", params=GUAIBA)
    relogio[0] += 29 * 60
    hotspots.respond(200, content=FIXTURE.read_bytes())  # o hotspots.geojson vence em 5 min
    cliente.get("/api/clima", params=GUAIBA)
    assert rota_meteomatics.call_count == 1

    relogio[0] += 60
    cliente.get("/api/clima", params=GUAIBA)
    assert rota_meteomatics.call_count == 2


def test_cada_hotspot_tem_o_proprio_cache(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route
) -> None:
    rota_meteomatics.respond(200, json=meteomatics())

    cliente.get("/api/clima", params=GUAIBA)
    cliente.get("/api/clima", params=coordenadas("kariba"))
    cliente.get("/api/clima", params=GUAIBA)

    assert rota_meteomatics.call_count == 2


def test_resposta_da_reserva_tambem_fica_em_cache(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route, rota_snapshot: respx.Route,
) -> None:
    # Com as fontes ao vivo fora, cada pedido levaria até 10 s de timeouts.
    rota_meteomatics.respond(500)
    rota_open_meteo.respond(500)
    rota_snapshot.respond(200, json=SNAPSHOT)

    cliente.get("/api/clima", params=GUAIBA)
    segunda = cliente.get("/api/clima", params=GUAIBA)

    assert segunda.json()["source"] == "snapshot"
    assert rota_meteomatics.call_count == rota_open_meteo.call_count == 1


def test_falha_total_nao_fica_em_cache(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route, rota_snapshot: respx.Route,
) -> None:
    rota_meteomatics.respond(500)
    rota_open_meteo.respond(500)
    rota_snapshot.respond(404)
    assert cliente.get("/api/clima", params=GUAIBA).status_code == 503

    rota_meteomatics.respond(200, json=meteomatics())

    assert cliente.get("/api/clima", params=GUAIBA).json()["source"] == "meteomatics"


# ---------- previsao_tempo chama a mesma função, sem HTTP ----------


def test_previsao_tempo_usa_a_cadeia_sem_passar_pela_api_clima(
    hotspots: respx.Route, rota_meteomatics: respx.Route, respx_mock: respx.MockRouter
) -> None:
    rota_meteomatics.respond(200, json=meteomatics())

    resultado = previsao_tempo("guaiba-rs")

    assert resultado["dias"] == DIAS
    assert resultado["fonte"]["nome"] == "Meteomatics"
    # Só o hotspots.geojson e a Meteomatics: nenhum GET HTTP para uma /api/clima.
    assert [c.request.url.host for c in respx_mock.calls] == ["dados.teste", "api.meteomatics.com"]


def test_previsao_tempo_e_api_clima_dividem_o_cache(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route
) -> None:
    rota_meteomatics.respond(200, json=meteomatics())

    http = cliente.get("/api/clima", params=GUAIBA).json()
    ferramenta = previsao_tempo("guaiba-rs", 3)

    assert ferramenta["dias"] == http["days"]
    assert ferramenta["atualizada_em"] == http["updated_at"]
    assert rota_meteomatics.call_count == 1


# ---------- validação do pedido ----------


def test_hotspot_fora_do_hotspots_geojson_da_422_sem_consultar_fontes(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route,
) -> None:
    resposta = cliente.get("/api/clima", params={**GUAIBA, "hotspot": "lago-inventado"})

    assert resposta.status_code == 422
    erro = resposta.json()["detail"][0]
    assert erro["type"] == "hotspot_desconhecido"
    assert erro["loc"] == ["query", "hotspot"]
    assert erro["ctx"]["validos"] == [
        "norte-fire-ca", "costa-peru", "norte-amazonia", "guaiba-rs", "kariba", "borneo",
    ]
    assert "lago-inventado" not in resposta.text
    assert rota_meteomatics.call_count == rota_open_meteo.call_count == 0


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"lat": "-30.0", "lon": "-51.2"}, id="sem-hotspot"),
        pytest.param({**GUAIBA, "hotspot": "../hotspots"}, id="hotspot-caminho"),
        pytest.param({**GUAIBA, "hotspot": "Guaiba RS"}, id="hotspot-fora-do-formato"),
        pytest.param({**GUAIBA, "hotspot": "a" * 65}, id="hotspot-longo"),
        pytest.param({"hotspot": "guaiba-rs", "lon": "-51.2"}, id="sem-lat"),
        pytest.param({"hotspot": "guaiba-rs", "lat": "-30.0"}, id="sem-lon"),
        pytest.param({**GUAIBA, "lat": "abc"}, id="lat-texto"),
        pytest.param({**GUAIBA, "lat": "nan"}, id="lat-nan"),
        pytest.param({**GUAIBA, "lon": "inf"}, id="lon-inf"),
        pytest.param({**GUAIBA, "lat": "91"}, id="lat-fora-da-faixa"),
        pytest.param({**GUAIBA, "lon": "-181"}, id="lon-fora-da-faixa"),
    ],
)
def test_pedido_fora_do_formato_da_422_sem_ler_nada(
    cliente: TestClient, respx_mock: respx.MockRouter, params: dict[str, str]
) -> None:
    resposta = cliente.get("/api/clima", params=params)

    assert resposta.status_code == 422
    assert not respx_mock.calls


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({**GUAIBA, "lat": "-23.5"}, id="lat-de-outro-lugar"),
        pytest.param({**GUAIBA, "lon": "-46.6"}, id="lon-de-outro-lugar"),
        pytest.param({**coordenadas("kariba"), "hotspot": "guaiba-rs"}, id="coordenadas-de-outro-hotspot"),
    ],
)
def test_coordenadas_que_nao_sao_do_hotspot_dao_422_sem_consultar_fontes(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route,
    rota_open_meteo: respx.Route, params: dict[str, str],
) -> None:
    # Sem isso, a /api/clima viraria um proxy da conta da Meteomatics para qualquer ponto.
    resposta = cliente.get("/api/clima", params=params)

    assert resposta.status_code == 422
    assert resposta.json()["detail"][0]["type"] == "coordenadas_do_hotspot"
    assert rota_meteomatics.call_count == rota_open_meteo.call_count == 0


def test_coordenadas_como_o_app_manda_sao_aceitas(
    cliente: TestClient, hotspots: respx.Route, rota_meteomatics: respx.Route
) -> None:
    # String(-30.0) no JavaScript é "-30".
    rota_meteomatics.respond(200, json=meteomatics())

    resposta = cliente.get("/api/clima", params={"hotspot": "guaiba-rs", "lat": "-30", "lon": "-51.2"})

    assert resposta.status_code == 200


@pytest.mark.parametrize(
    "falha",
    [httpx.Response(500), httpx.ConnectTimeout("demorou"), httpx.Response(200, text="{}")],
    ids=["http-500", "timeout", "fora-do-contrato"],
)
def test_sem_hotspots_geojson_da_503_sem_consultar_fontes(
    cliente: TestClient, respx_mock: respx.MockRouter, rota_meteomatics: respx.Route,
    falha: httpx.Response | Exception,
) -> None:
    falhar(respx_mock.get(URL_HOTSPOTS), falha)

    resposta = cliente.get("/api/clima", params=GUAIBA)

    assert resposta.status_code == 503
    assert rota_meteomatics.call_count == 0


def test_so_get(cliente: TestClient, respx_mock: respx.MockRouter) -> None:
    assert cliente.post("/api/clima", params=GUAIBA).status_code == 405
    assert not respx_mock.calls


# ---------- aquecimento do cache na subida ----------

IDS = ["norte-fire-ca", "costa-peru", "norte-amazonia", "guaiba-rs", "kariba", "borneo"]


@pytest.fixture
def com_aquecimento(monkeypatch: pytest.MonkeyPatch) -> None:
    # O conftest desliga o aquecimento em todos os testes; estes o religam.
    monkeypatch.setattr(main, "aquecer_previsoes", ferramentas.aquecer_previsoes)


def esperar(condicao: Any, prazo_s: float = 3.0) -> bool:
    fim = time.monotonic() + prazo_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.01)
    return condicao()


def test_aquecer_busca_os_6_hotspots_e_enche_o_cache(
    hotspots: respx.Route, rota_meteomatics: respx.Route
) -> None:
    rota_meteomatics.respond(200, json=meteomatics())

    fontes = ferramentas.aquecer_previsoes()

    assert fontes == dict.fromkeys(IDS, "meteomatics")
    assert rota_meteomatics.call_count == 6
    guaiba = ferramentas.hotspots_por_id()["guaiba-rs"]  # type: ignore[index]
    ferramentas.previsao_do_hotspot(guaiba)
    assert rota_meteomatics.call_count == 6


def test_aquecer_marca_o_hotspot_sem_previsao_e_segue(
    hotspots: respx.Route, rota_meteomatics: respx.Route, rota_open_meteo: respx.Route,
    rota_snapshot: respx.Route,
) -> None:
    rota_meteomatics.respond(500)
    rota_open_meteo.respond(500)
    rota_snapshot.respond(200, json=SNAPSHOT)  # só tem o guaiba-rs

    fontes = ferramentas.aquecer_previsoes()

    assert fontes == {**dict.fromkeys(IDS, "indisponível"), "guaiba-rs": "snapshot"}


def test_aquecer_sem_hotspots_geojson_nao_consulta_fontes(
    respx_mock: respx.MockRouter, rota_meteomatics: respx.Route, rota_open_meteo: respx.Route
) -> None:
    respx_mock.get(URL_HOTSPOTS).respond(503)

    assert ferramentas.aquecer_previsoes() == {}
    assert rota_meteomatics.call_count == rota_open_meteo.call_count == 0


def test_subida_aquece_o_cache_em_segundo_plano(
    com_aquecimento: None, hotspots: respx.Route, rota_meteomatics: respx.Route
) -> None:
    rota_meteomatics.respond(200, json=meteomatics())

    with TestClient(main.app) as cliente:
        assert esperar(lambda: rota_meteomatics.call_count == 6)
        resposta = cliente.get("/api/clima", params=GUAIBA)

    assert resposta.json()["source"] == "meteomatics"
    assert rota_meteomatics.call_count == 6  # o pedido já achou o cache cheio


def test_health_responde_sem_esperar_o_aquecimento(monkeypatch: pytest.MonkeyPatch) -> None:
    comecou, liberar, terminou = threading.Event(), threading.Event(), threading.Event()

    def aquecimento_lento() -> dict[str, str]:
        comecou.set()
        liberar.wait(3)
        terminou.set()
        return {}

    monkeypatch.setattr(main, "aquecer_previsoes", aquecimento_lento)
    try:
        inicio = time.monotonic()
        with TestClient(main.app) as cliente:
            assert comecou.wait(2)
            resposta = cliente.get("/health")
            # A subida e o /health não esperaram o aquecimento terminar.
            assert not terminou.is_set()
            assert time.monotonic() - inicio < 1
            assert resposta.json() == {"ok": True}
    finally:
        liberar.set()


def test_falha_no_aquecimento_nao_derruba_a_subida(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def quebrar() -> dict[str, str]:
        raise RuntimeError("segredo interno")

    monkeypatch.setattr(main, "aquecer_previsoes", quebrar)

    with caplog.at_level(logging.WARNING, logger="cass.main"), TestClient(main.app) as cliente:
        assert esperar(lambda: "aquecimento das previsões falhou" in caplog.text)
        assert cliente.get("/health").status_code == 200

    assert "RuntimeError" in caplog.text
    assert "segredo interno" not in caplog.text


def test_pedido_durante_a_busca_do_mesmo_hotspot_espera_em_vez_de_repetir(
    hotspots: respx.Route, rota_meteomatics: respx.Route
) -> None:
    dentro, liberar = threading.Event(), threading.Event()

    def meteomatics_lenta(request: httpx.Request) -> httpx.Response:
        dentro.set()
        liberar.wait(5)
        return httpx.Response(200, json=meteomatics())

    rota_meteomatics.mock(side_effect=meteomatics_lenta)
    guaiba = ferramentas.hotspots_por_id()["guaiba-rs"]  # type: ignore[index]
    resultados: list[str] = []

    def buscar() -> None:
        resultados.append(ferramentas.previsao_do_hotspot(guaiba).previsao.source)

    aquecimento = threading.Thread(target=buscar)
    aquecimento.start()
    assert dentro.wait(2)
    pedido = threading.Thread(target=buscar)
    pedido.start()
    time.sleep(0.05)  # o pedido chega enquanto a Meteomatics ainda não respondeu
    liberar.set()
    aquecimento.join(2)
    pedido.join(2)

    assert resultados == ["meteomatics", "meteomatics"]
    assert rota_meteomatics.call_count == 1
