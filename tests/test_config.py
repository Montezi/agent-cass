"""Testes da leitura de configuração."""

import pytest
from fastapi.testclient import TestClient

from cass.config import Configuracao, ErroDeConfiguracao, carregar_config
from cass.main import app


def test_falha_sem_projeto() -> None:
    with pytest.raises(ErroDeConfiguracao, match="GOOGLE_CLOUD_PROJECT"):
        carregar_config({})


def test_falha_com_projeto_vazio() -> None:
    with pytest.raises(ErroDeConfiguracao, match="GOOGLE_CLOUD_PROJECT"):
        carregar_config({"GOOGLE_CLOUD_PROJECT": "   "})


def test_padroes_seguros_com_so_o_projeto() -> None:
    config = carregar_config({"GOOGLE_CLOUD_PROJECT": "projeto-teste"})

    assert config == Configuracao(
        google_cloud_project="projeto-teste",
        google_cloud_location="global",
        gemini_model="gemini-3.8-flash",
        app_origin="http://localhost:5173",
        dados_base="http://localhost:5173/dados",
        decider_ativo=False,
        decider_url="http://127.0.0.1:8099/v1/systemone",
        aquecer_previsoes=False,
    )


def test_variavel_vazia_usa_o_padrao() -> None:
    config = carregar_config({"GOOGLE_CLOUD_PROJECT": "p", "GEMINI_MODEL": ""})

    assert config.gemini_model == "gemini-3.8-flash"


def test_le_valores_do_ambiente() -> None:
    config = carregar_config(
        {
            "GOOGLE_CLOUD_PROJECT": "p",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
            "DADOS_BASE": "https://exemplo.org/dados",
        }
    )

    assert config.google_cloud_location == "us-central1"
    assert config.dados_base == "https://exemplo.org/dados"


@pytest.mark.parametrize("valor", ["true", "TRUE", "1", "sim", "yes"])
def test_decider_liga_com_valor_explicito(valor: str) -> None:
    config = carregar_config({"GOOGLE_CLOUD_PROJECT": "p", "DECIDER_ATIVO": valor})

    assert config.decider_ativo is True


@pytest.mark.parametrize("valor", ["false", "0", "", "ture", "talvez"])
def test_decider_fica_desligado_em_qualquer_outro_valor(valor: str) -> None:
    config = carregar_config({"GOOGLE_CLOUD_PROJECT": "p", "DECIDER_ATIVO": valor})

    assert config.decider_ativo is False


def test_aquecimento_das_previsoes_fica_desligado_por_padrao() -> None:
    assert carregar_config({"GOOGLE_CLOUD_PROJECT": "p"}).aquecer_previsoes is False


@pytest.mark.parametrize("valor", ["true", "TRUE", "1", "sim", "yes"])
def test_aquecimento_liga_com_valor_explicito(valor: str) -> None:
    config = carregar_config({"GOOGLE_CLOUD_PROJECT": "p", "AQUECER_PREVISOES": valor})

    assert config.aquecer_previsoes is True


@pytest.mark.parametrize("valor", ["false", "0", "", "ture", "talvez"])
def test_aquecimento_fica_desligado_em_qualquer_outro_valor(valor: str) -> None:
    config = carregar_config({"GOOGLE_CLOUD_PROJECT": "p", "AQUECER_PREVISOES": valor})

    assert config.aquecer_previsoes is False


def test_app_nao_sobe_sem_projeto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    with pytest.raises(ErroDeConfiguracao):
        with TestClient(app):
            pass
