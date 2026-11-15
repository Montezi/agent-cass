"""Testes do endpoint de saúde."""

import pytest
from fastapi.testclient import TestClient

from cass.main import app


def test_health_devolve_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")

    # O "with" roda o lifespan, que carrega a configuração na subida.
    with TestClient(app) as cliente:
        resposta = cliente.get("/health")

    assert resposta.status_code == 200
    assert resposta.json() == {"ok": True}
