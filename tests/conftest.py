"""Fixtures compartilhadas pelos testes."""

import pytest

from cass import main

# Os ids de tests/fixtures/hotspots.geojson.
IDS_DO_FIXTURE = ["norte-fire-ca", "costa-peru", "norte-amazonia", "guaiba-rs", "kariba", "borneo"]


@pytest.fixture
def ids_conhecidos(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """O endpoint confere o hotspot_id contra estes ids, sem ler o hotspots.geojson.

    Para testes que chamam o /api/agente sem servir dados: sem isto, a conferência
    tentaria ler o DADOS_BASE de verdade.
    """
    monkeypatch.setattr(main, "ids_dos_hotspots", lambda: list(IDS_DO_FIXTURE))
    return IDS_DO_FIXTURE
