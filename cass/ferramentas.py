"""Ferramentas do Cass (@tool do Strands). Todas só de leitura.

Leem só {DADOS_BASE}/hotspots.geojson e {CLIMA_URL}, sempre com o id conferido contra
a lista do hotspots.geojson. Falhas voltam como dado ({"erro": ...}), para o Gemini se
corrigir, em vez de virar exceção. As fontes da resposta são montadas pelo código a
partir do campo "fonte" (ou "fontes") de cada resultado: {"nome", "data", "url"}.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import wraps
from time import monotonic
from typing import Any, ParamSpec, TypeVar, get_args

import httpx
from pydantic import BaseModel, ValidationError
from strands import tool

from cass.config import carregar_config
from cass.formato import AVISO_DE_EXEMPLO, Idioma, formatar_data, formatar_dia
from cass.formato import formatar_fonte, formatar_numero
from cass.modelos import ClasseDominante, ColecaoHotspots, Hotspot, PrevisaoClima

TIMEOUT_S = 5.0
CACHE_TTL_S = 300.0  # o hotspots.geojson só muda num deploy do frontend
CACHE_CLIMA_TTL_S = 600.0  # evita repetir a /api/clima quando o alerta usa a previsão
DIAS_MIN, DIAS_MAX, DIAS_PADRAO = 1, 7, 3
PREVISAO_VELHA = timedelta(days=3)  # api-contract.md: só referência, nunca dispara aviso
SEM_STATS = {"disponivel": False, "motivo": "ainda não calculado pelo pipeline"}
CLASSES: list[str] = list(get_args(ClasseDominante))
IDIOMAS: list[str] = ["pt", "en"]
FONTE_RADAR = "NISAR"
FONTE_CONTEXTO = "NOAA CPC (contexto regional)"  # do enso_effect
FONTES_DE_PREVISAO = {"meteomatics": "Meteomatics", "open-meteo": "Open-Meteo", "snapshot": "Snapshot"}
ROTULOS_DAS_CLASSES: dict[str, dict[str, str]] = {  # i18n do frontend (pt.json, en.json)
    "pt": {
        "flood": "Cheia",
        "drought": "Seca",
        "fire_deforestation": "Fogo e desmatamento",
        "deformation": "Deformação",
        "stable": "Estável ou incerto",
    },
    "en": {
        "flood": "Flood",
        "drought": "Drought",
        "fire_deforestation": "Fire and deforestation",
        "deformation": "Deformation",
        "stable": "Stable or uncertain",
    },
}

P = ParamSpec("P")
M = TypeVar("M", bound=BaseModel)


# ---------- erros como dado ----------


class _Recusa(Exception):
    """Resultado de erro de uma ferramenta, devolvido ao modelo como dado."""

    def __init__(self, **dados: Any) -> None:
        super().__init__(dados.get("erro"))
        self.dados = dados


def _erros_como_dado(funcao: Callable[P, dict[str, Any]]) -> Callable[P, dict[str, Any]]:
    @wraps(funcao)
    def envolvida(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        try:
            return funcao(*args, **kwargs)
        except _Recusa as recusa:
            return recusa.dados

    return envolvida


# ---------- leitura com cache ----------


@dataclass(frozen=True)
class _Guardado:
    valor: Any
    validade: float  # instante (monotonic) em que vence


_cache: dict[str, _Guardado] = {}


def limpar_cache() -> None:
    _cache.clear()


def _agora() -> datetime:
    return datetime.now(UTC)


def _ler(url: str, nome: str, modelo: type[M], ttl: float) -> M:
    """GET com timeout, validado contra o contrato; fica em cache por `ttl` segundos."""
    guardado = _cache.get(url)
    if guardado is not None and monotonic() < guardado.validade:
        return guardado.valor
    try:
        resposta = httpx.get(url, timeout=TIMEOUT_S)
        resposta.raise_for_status()
        valor = modelo.model_validate_json(resposta.content)
    except httpx.TimeoutException as e:
        raise _Recusa(erro=f"{nome} não respondeu em {TIMEOUT_S:g} s", url=url) from e
    except httpx.HTTPStatusError as e:
        raise _Recusa(erro=f"não consegui ler {nome} (HTTP {e.response.status_code})", url=url) from e
    except httpx.HTTPError as e:
        raise _Recusa(erro=f"não consegui ler {nome} ({type(e).__name__})", url=url) from e
    except ValidationError as e:
        raise _Recusa(
            erro=f"{nome} não segue o contrato de dados ({e.error_count()} erros)", url=url
        ) from e
    _cache[url] = _Guardado(valor, monotonic() + ttl)
    return valor


def _colecao() -> tuple[ColecaoHotspots, str]:
    url = f"{carregar_config().dados_base.rstrip('/')}/hotspots.geojson"
    return _ler(url, "o hotspots.geojson", ColecaoHotspots, CACHE_TTL_S), url


def _hotspot(id: str) -> tuple[Hotspot, ColecaoHotspots, str]:
    """O hotspot com esse id; id desconhecido vira {"erro", "validos"}."""
    colecao, url = _colecao()
    for hotspot in colecao.features:
        if hotspot.properties.id == id:
            return hotspot, colecao, url
    raise _Recusa(
        erro=f"hotspot desconhecido: {id!r}",
        validos=[h.properties.id for h in colecao.features],
    )


def _fonte(nome: str, data: date | None, url: str) -> dict[str, Any]:
    return {"nome": nome, "data": data.isoformat() if data else None, "url": url}


# ---------- ferramentas ----------


@tool
@_erros_como_dado
def listar_hotspots() -> dict[str, Any]:
    """Lista os hotspots monitorados: id, nome em PT e EN e a classe dominante medida.

    Use para descobrir o id de um hotspot antes de consultá-lo.
    """
    colecao, url = _colecao()
    passagens = [h.properties.last_pass for h in colecao.features if h.properties.last_pass]
    return {
        "fonte": _fonte(FONTE_RADAR, max(passagens, default=None), url),
        "dados_de_exemplo": colecao.metadata.sample,
        "hotspots": [
            {"id": p.id, "name": p.name.model_dump(), "dominant_class": p.dominant_class}
            for p in (h.properties for h in colecao.features)
        ],
    }


@tool
@_erros_como_dado
def consultar_hotspot(id: str) -> dict[str, Any]:
    """Consulta um hotspot: classe dominante, área afetada, confiança, data da última
    passagem do radar, efeito esperado do El Niño e área de interesse.

    Valores null ainda não foram medidos.

    Args:
        id: id do hotspot, como "guaiba-rs" (os ids vêm de listar_hotspots).
    """
    hotspot, colecao, url = _hotspot(id)
    p = hotspot.properties
    return {
        "fonte": _fonte(FONTE_RADAR, p.last_pass, url),
        "dados_de_exemplo": colecao.metadata.sample,
        # mode="json": datas viram texto e os nulls continuam lá.
        "hotspot": p.model_dump(mode="json"),
    }


@tool
@_erros_como_dado
def explicar_classe(id: str, classe: str) -> dict[str, Any]:
    """Explica por que o radar deu essa classe ao hotspot: a regra da árvore de decisão
    e os valores medidos que a dispararam.

    Args:
        id: id do hotspot, como "guaiba-rs".
        classe: flood, drought, fire_deforestation, deformation ou stable.
    """
    _hotspot(id)
    if classe not in CLASSES:
        raise _Recusa(erro=f"classe desconhecida: {classe!r}", validos=CLASSES)
    # Vem do stats/<id>.json, que ainda não está no contrato de dados.
    return dict(SEM_STATS)


@tool
@_erros_como_dado
def hipotese(id: str) -> dict[str, Any]:
    """Hipótese para a mudança no hotspot: motor físico provável, evidência e força
    (fraca, moderada ou forte). É hipótese, não medição nem causa comprovada.

    Args:
        id: id do hotspot, como "guaiba-rs".
    """
    _hotspot(id)
    return dict(SEM_STATS)  # stats/<id>.json, ainda fora do contrato


@tool
@_erros_como_dado
def exposicao(id: str) -> dict[str, Any]:
    """Quem e o que está na área do hotspot: pessoas, hospitais, escolas e estradas.

    Args:
        id: id do hotspot, como "guaiba-rs".
    """
    _hotspot(id)
    return dict(SEM_STATS)  # stats/<id>.json, ainda fora do contrato


@tool
@_erros_como_dado
def contexto_enso(id: str) -> dict[str, Any]:
    """Efeito esperado do El Niño na região do hotspot. É tendência, não garantia, e
    não é medição: nunca diga que o El Niño causou a mudança.

    Args:
        id: id do hotspot (a região), como "guaiba-rs".
    """
    hotspot, colecao, url = _hotspot(id)
    efeito = hotspot.properties.enso_effect
    return {
        "fonte": _fonte(FONTE_CONTEXTO, None, url),
        "dados_de_exemplo": colecao.metadata.sample,
        "hotspot_id": id,
        "enso_effect": efeito.model_dump() if efeito else None,
    }


@tool
@_erros_como_dado
def previsao_tempo(id: str, dias: int = DIAS_PADRAO) -> dict[str, Any]:
    """Previsão de chuva por dia no hotspot, com a fonte e a data em que foi gerada.

    Se so_referencia for true, a previsão tem mais de 3 dias: mostre só como
    referência e não a use para avisar de chuva.

    Args:
        id: id do hotspot, como "guaiba-rs".
        dias: quantos dias, de 1 a 7.
    """
    if not DIAS_MIN <= dias <= DIAS_MAX:
        raise _Recusa(erro=f"dias deve ser de {DIAS_MIN} a {DIAS_MAX}", validos=list(range(1, 8)))
    hotspot, _, _ = _hotspot(id)
    lon, lat = hotspot.geometry.coordinates
    url = str(httpx.URL(carregar_config().clima_url, params={"hotspot": id, "lat": lat, "lon": lon}))
    previsao = _ler(url, "a /api/clima", PrevisaoClima, CACHE_CLIMA_TTL_S)
    em_json = previsao.model_dump(mode="json")
    return {
        "fonte": _fonte(FONTES_DE_PREVISAO[previsao.source], previsao.updated_at.date(), url),
        "hotspot_id": id,
        "atualizada_em": em_json["updated_at"],
        "so_referencia": _agora() - previsao.updated_at > PREVISAO_VELHA,
        "dias": em_json["days"][:dias],
    }


@tool
@_erros_como_dado
def rascunhar_alerta(id: str, idioma: str = "pt") -> dict[str, Any]:
    """Monta o rascunho de um alerta para WhatsApp: medido (com data), previsão, fontes e
    o link do app. Só monta o texto com o que as outras ferramentas devolvem; nada é
    enviado.

    Args:
        id: id do hotspot, como "guaiba-rs".
        idioma: pt ou en.
    """
    if idioma not in IDIOMAS:
        raise _Recusa(erro=f"idioma desconhecido: {idioma!r}", validos=IDIOMAS)
    dados = consultar_hotspot(id)
    if "erro" in dados:
        return dados
    previsao = previsao_tempo(id, DIAS_PADRAO)
    # hipotese(id) e exposicao(id) entram no texto quando o stats/<id>.json estiver no
    # contrato; hoje só devolvem "ainda não calculado".
    fontes = [dados["fonte"], *([previsao["fonte"]] if "fonte" in previsao else [])]
    link = f"{carregar_config().app_origin.rstrip('/')}/?hotspot={id}"
    return {
        "rascunho": _texto_do_alerta(dados, previsao, fontes, link, idioma),  # type: ignore[arg-type]
        "enviado": False,
        "dados_de_exemplo": dados["dados_de_exemplo"],
        "fontes": fontes,
    }


# ---------- texto do alerta ----------

TEXTOS_DO_ALERTA: dict[str, dict[str, str]] = {
    "pt": {
        "titulo": "Alerta CassAAndra: {nome}",
        "sem_medicao": "Medido: o radar ainda não tem medição para esta área.",
        "medido": "Medido: {frase}.",
        "mudanca": "Mudança",
        "area": " em {area} km²",
        "confianca": " (confiança de {pct}%)",
        "passagem": ", passagem do radar de {data}",
        "chuva": "Previsão de chuva: {dias}.",
        "dia": "{mm} mm em {dia}",
        "previsao_velha": "Previsão de {data}, só para referência.",
        "sem_previsao": "Previsão: indisponível agora.",
        "fontes": "Fonte: {fontes}.",
        "link": "Veja no app: {link}",
    },
    "en": {
        "titulo": "CassAAndra alert: {nome}",
        "sem_medicao": "Measured: the radar has no measurement for this area yet.",
        "medido": "Measured: {frase}.",
        "mudanca": "Change",
        "area": " over {area} km²",
        "confianca": " ({pct}% confidence)",
        "passagem": ", radar pass of {data}",
        "chuva": "Rain forecast: {dias}.",
        "dia": "{mm} mm on {dia}",
        "previsao_velha": "Forecast from {data}, for reference only.",
        "sem_previsao": "Forecast: unavailable right now.",
        "fontes": "Source: {fontes}.",
        "link": "See it in the app: {link}",
    },
}


def _texto_do_alerta(
    dados: dict[str, Any],
    previsao: dict[str, Any],
    fontes: list[dict[str, Any]],
    link: str,
    idioma: Idioma,
) -> str:
    """Tom calmo, sem afirmar causa; todo número vem dos resultados das ferramentas."""
    t = TEXTOS_DO_ALERTA[idioma]
    p = dados["hotspot"]
    linhas = [t["titulo"].format(nome=p["name"][idioma]), _linha_medido(p, idioma)]
    if "erro" in previsao:
        linhas.append(t["sem_previsao"])
    elif previsao["so_referencia"]:
        linhas.append(t["previsao_velha"].format(data=formatar_data(previsao["atualizada_em"], idioma)))
    else:
        dias = ", ".join(
            t["dia"].format(
                mm=formatar_numero(d["precipitation_mm"], idioma), dia=formatar_dia(d["date"], idioma)
            )
            for d in previsao["dias"]
        )
        linhas.append(t["chuva"].format(dias=dias))
    if dados["dados_de_exemplo"]:
        linhas.append(AVISO_DE_EXEMPLO[idioma])
    linhas.append(
        t["fontes"].format(
            fontes="; ".join(formatar_fonte(f["nome"], f["data"], idioma) for f in fontes)
        )
    )
    linhas.append(t["link"].format(link=link))
    return "\n".join(linhas)


def _linha_medido(p: dict[str, Any], idioma: Idioma) -> str:
    t = TEXTOS_DO_ALERTA[idioma]
    classe, area = p["dominant_class"], p["affected_area_km2"]
    confianca, passagem = p["confidence"], p["last_pass"]
    if classe is None and area is None and confianca is None and passagem is None:
        return t["sem_medicao"]
    frase = ROTULOS_DAS_CLASSES[idioma][classe] if classe else t["mudanca"]
    if area is not None:
        frase += t["area"].format(area=formatar_numero(area, idioma))
    if confianca is not None:
        frase += t["confianca"].format(pct=round(confianca * 100))
    if passagem is not None:
        frase += t["passagem"].format(data=formatar_data(passagem, idioma))
    return t["medido"].format(frase=frase)
