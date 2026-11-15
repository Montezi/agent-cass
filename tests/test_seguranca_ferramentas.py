"""Fronteira HTTP das ferramentas, sem rede real ou modelo de linguagem."""

import gzip
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from cass import ferramentas

BASE = "https://dados.teste/dados"
URL = f"{BASE}/hotspots.geojson"
FIXTURE = Path(__file__).parent / "fixtures" / "hotspots.geojson"


@pytest.fixture(autouse=True)
def ambiente(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    monkeypatch.setenv("DADOS_BASE", BASE)
    ferramentas.limpar_cache()
    yield
    ferramentas.limpar_cache()


class Fluxo(httpx.SyncByteStream):
    def __init__(self, blocos: list[bytes]) -> None:
        self.blocos = blocos
        self.lidos = 0
        self.fechado = False

    def __iter__(self) -> Iterator[bytes]:
        for bloco in self.blocos:
            self.lidos += 1
            yield bloco

    def close(self) -> None:
        self.fechado = True


def test_recusa_corpo_declarado_grande_sem_consumir_stream(respx_mock: respx.MockRouter) -> None:
    fluxo = Fluxo([b"nao deve ser lido"])
    respx_mock.get(URL).respond(200, headers={"Content-Length": str(ferramentas.MAX_BYTES_DADOS + 1)}, stream=fluxo)

    resultado = ferramentas.listar_hotspots()

    assert "excede o limite" in resultado["erro"]
    assert fluxo.lidos == 0 and fluxo.fechado
    assert URL not in ferramentas._cache


def test_recusa_corpo_chunked_grande_e_fecha_stream(respx_mock: respx.MockRouter) -> None:
    fluxo = Fluxo([b"x" * ferramentas.MAX_BYTES_DADOS, b"x", b"nao deve ser lido"])
    respx_mock.get(URL).respond(200, stream=fluxo)

    resultado = ferramentas.listar_hotspots()

    assert "excede o limite" in resultado["erro"]
    assert fluxo.lidos == 2 and fluxo.fechado
    assert URL not in ferramentas._cache


def test_recusa_gzip_antes_de_descomprimir(respx_mock: respx.MockRouter) -> None:
    fluxo = Fluxo([gzip.compress(b"x" * (ferramentas.MAX_BYTES_DADOS + 1))])
    rota = respx_mock.get(URL).respond(200, headers={"Content-Encoding": "gzip"}, stream=fluxo)

    resultado = ferramentas.listar_hotspots()

    assert "codificação não permitida" in resultado["erro"]
    assert rota.calls.last.request.headers["Accept-Encoding"] == "identity"
    assert fluxo.lidos == 0 and fluxo.fechado


def test_redirect_nao_consulta_destino_alternativo(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).respond(302, headers={"Location": "http://169.254.169.254/metadata"})

    resultado = ferramentas.listar_hotspots()

    assert "HTTP 302" in resultado["erro"]
    assert len(respx_mock.calls) == 1


def test_stream_continuo_nao_renova_prazo_total(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tempo = [100.0]

    class FluxoLento(Fluxo):
        def __iter__(self) -> Iterator[bytes]:
            for bloco in super().__iter__():
                tempo[0] += 2.0
                yield bloco

    monkeypatch.setattr(ferramentas, "monotonic", lambda: tempo[0])
    fluxo = FluxoLento([b" "] * 20)
    respx_mock.get(URL).respond(200, stream=fluxo)

    resultado = ferramentas.listar_hotspots()

    assert "não respondeu em 5 s" in resultado["erro"]
    assert fluxo.lidos == 3 and fluxo.fechado


@pytest.mark.parametrize("dias", [True, False, 1.5, "3", None, [], {}])
def test_dias_invalido_recusado_antes_de_qualquer_http(respx_mock: respx.MockRouter, dias: object) -> None:
    resultado = ferramentas.previsao_tempo("guaiba-rs", dias)

    assert "dias deve ser" in resultado["erro"]
    assert not respx_mock.calls


@pytest.mark.parametrize("id", ["../segredo", "https://malicioso.invalid", "guaiba-rs&lat=0", "guaiba-rs/../../x"])
def test_id_nao_pode_mudar_url(respx_mock: respx.MockRouter, id: str) -> None:
    respx_mock.get(URL).respond(200, content=FIXTURE.read_bytes())

    resultado = ferramentas.previsao_tempo(id)

    assert "hotspot desconhecido" in resultado["erro"]
    assert len(respx_mock.calls) == 1
    assert str(respx_mock.calls.last.request.url) == URL
