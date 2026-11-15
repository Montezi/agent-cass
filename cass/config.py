"""Leitura das variáveis de ambiente do serviço.

Quem carrega o .env é o uvicorn (--env-file .env); este módulo só lê o ambiente.
Os padrões espelham o .env.example.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

PADROES: dict[str, str] = {
    "GOOGLE_CLOUD_LOCATION": "global",
    "GEMINI_MODEL": "gemini-3.8-flash",
    "APP_ORIGIN": "http://localhost:5173",
    "DADOS_BASE": "http://localhost:5173/dados",
    "DECIDER_URL": "http://127.0.0.1:8099/v1/systemone",
}

# Só estes valores ligam o Decider e o aquecimento; qualquer outro (inclusive erro de
# digitação) desliga.
VERDADEIROS = frozenset({"true", "1", "sim", "yes"})


class ErroDeConfiguracao(RuntimeError):
    """Configuração obrigatória ausente ou inválida."""


@dataclass(frozen=True)
class Configuracao:
    google_cloud_project: str
    google_cloud_location: str
    gemini_model: str
    app_origin: str
    dados_base: str
    decider_ativo: bool
    decider_url: str
    # Opcionais: sem os dois, a previsão começa na Open-Meteo. Fora do repr, para a
    # senha não aparecer num log da configuração.
    meteomatics_user: str = field(default="", repr=False)
    meteomatics_password: str = field(default="", repr=False)
    # Buscar a previsão dos hotspots na subida, em segundo plano. Desligado por padrão:
    # com --cpu-throttling, o Cloud Run só dá CPU durante um pedido (veja cass/main.py).
    aquecer_previsoes: bool = False


def _ler(env: Mapping[str, str], nome: str) -> str:
    """Valor da variável sem espaços; vazia conta como ausente e cai no padrão."""
    valor = env.get(nome, "").strip()
    return valor or PADROES.get(nome, "")


def _exigir_projeto(env: Mapping[str, str]) -> str:
    projeto = _ler(env, "GOOGLE_CLOUD_PROJECT")
    if not projeto:
        raise ErroDeConfiguracao(
            "GOOGLE_CLOUD_PROJECT não está definida. Copie o .env.example para .env, "
            "preencha o ID do projeto do Google Cloud e rode com --env-file .env "
            "(no Cloud Run, defina a variável no serviço)."
        )
    return projeto


def carregar_config(env: Mapping[str, str] | None = None) -> Configuracao:
    """Monta a configuração a partir do ambiente (os.environ por padrão)."""
    env = os.environ if env is None else env
    return Configuracao(
        google_cloud_project=_exigir_projeto(env),
        google_cloud_location=_ler(env, "GOOGLE_CLOUD_LOCATION"),
        gemini_model=_ler(env, "GEMINI_MODEL"),
        app_origin=_ler(env, "APP_ORIGIN"),
        dados_base=_ler(env, "DADOS_BASE"),
        decider_ativo=_ler(env, "DECIDER_ATIVO").lower() in VERDADEIROS,
        decider_url=_ler(env, "DECIDER_URL"),
        meteomatics_user=_ler(env, "METEOMATICS_USER"),
        meteomatics_password=_ler(env, "METEOMATICS_PASSWORD"),
        aquecer_previsoes=_ler(env, "AQUECER_PREVISOES").lower() in VERDADEIROS,
    )
