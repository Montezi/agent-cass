"""Fixtures compartilhadas pelos testes."""

import pytest

from cass import main

# Os ids de tests/fixtures/hotspots.geojson.
IDS_DO_FIXTURE = ["norte-fire-ca", "costa-peru", "norte-amazonia", "guaiba-rs", "kariba", "borneo"]


@pytest.fixture(autouse=True)
def sem_aquecimento(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada TestClient sobe o serviço; se AQUECER_PREVISOES estiver ligada no
    ambiente de quem roda os testes, a subida buscaria a previsão dos hotspots em
    segundo plano e gastaria as rotas do respx de cada teste.
    Os testes do aquecimento (tests/test_clima.py) religam a variável e a função.
    """
    monkeypatch.setattr(main, "aquecer_previsoes", dict)


@pytest.fixture
def ids_conhecidos(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """O endpoint confere o hotspot_id contra estes ids, sem ler o hotspots.geojson.

    Para testes que chamam o /api/agente sem servir dados: sem isto, a conferência
    tentaria ler o DADOS_BASE de verdade.
    """
    monkeypatch.setattr(main, "ids_dos_hotspots", lambda: list(IDS_DO_FIXTURE))
    return IDS_DO_FIXTURE
