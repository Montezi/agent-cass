"""Ferramentas do Cass (@tool do Strands). Todas só de leitura.

Leem {DADOS_BASE}/hotspots.geojson e nada mais. Falhas voltam como dado
({"erro": ...}), para o Gemini se corrigir, em vez de virar exceção.
"""

from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx
from pydantic import ValidationError
from strands import tool

from cass.config import carregar_config
from cass.modelos import ColecaoHotspots

TIMEOUT_S = 5.0
# O hotspots.geojson só muda num deploy do frontend; 5 min de cache bastam.
CACHE_TTL_S = 300.0


class ErroDeLeitura(Exception):
    """Não deu para ler ou validar o hotspots.geojson."""


@dataclass(frozen=True)
class _Cache:
    url: str
    colecao: ColecaoHotspots
    ids: tuple[str, ...]
    validade: float  # instante (monotonic) em que o cache vence


_cache: _Cache | None = None


def limpar_cache() -> None:
    global _cache
    _cache = None


def _url_hotspots() -> str:
    return f"{carregar_config().dados_base.rstrip('/')}/hotspots.geojson"


def _ler_hotspots(url: str) -> _Cache:
    """Coleção validada e seus ids; só lê de novo quando o cache vence ou a URL muda."""
    global _cache
    if _cache is not None and _cache.url == url and monotonic() < _cache.validade:
        return _cache
    try:
        resposta = httpx.get(url, timeout=TIMEOUT_S)
        resposta.raise_for_status()
        colecao = ColecaoHotspots.model_validate_json(resposta.content)
    except httpx.HTTPStatusError as e:
        raise ErroDeLeitura(
            f"não consegui ler o hotspots.geojson (HTTP {e.response.status_code})"
        ) from e
    except httpx.HTTPError as e:
        raise ErroDeLeitura(f"não consegui ler o hotspots.geojson ({type(e).__name__})") from e
    except ValidationError as e:
        raise ErroDeLeitura(
            f"o hotspots.geojson não segue o contrato de dados ({e.error_count()} erros)"
        ) from e
    ids = tuple(h.properties.id for h in colecao.features)
    _cache = _Cache(url, colecao, ids, monotonic() + CACHE_TTL_S)
    return _cache


@tool
def listar_hotspots() -> dict[str, Any]:
    """Lista os hotspots monitorados: id, nome em PT e EN e a classe dominante medida.

    Use para descobrir o id de um hotspot antes de consultá-lo.
    """
    url = _url_hotspots()
    try:
        dados = _ler_hotspots(url)
    except ErroDeLeitura as e:
        return {"erro": str(e), "fonte": url}
    return {
        "fonte": url,
        "dados_de_exemplo": dados.colecao.metadata.sample,
        "hotspots": [
            {"id": p.id, "name": p.name.model_dump(), "dominant_class": p.dominant_class}
            for p in (h.properties for h in dados.colecao.features)
        ],
    }


@tool
def consultar_hotspot(id: str) -> dict[str, Any]:
    """Consulta um hotspot: classe dominante, área afetada, confiança, data da última
    passagem do radar, efeito esperado do El Niño e área de interesse.

    Valores null ainda não foram medidos.

    Args:
        id: id do hotspot, como "guaiba-rs" (os ids vêm de listar_hotspots).
    """
    url = _url_hotspots()
    try:
        dados = _ler_hotspots(url)
    except ErroDeLeitura as e:
        return {"erro": str(e), "fonte": url}
    for hotspot in dados.colecao.features:
        if hotspot.properties.id == id:
            return {
                "fonte": url,
                "dados_de_exemplo": dados.colecao.metadata.sample,
                # mode="json": datas viram texto e os nulls continuam lá.
                "hotspot": hotspot.properties.model_dump(mode="json"),
            }
    return {"erro": f"hotspot desconhecido: {id!r}", "validos": list(dados.ids)}
