"""Ferramentas do Cass (@tool do Strands). Todas só de leitura.

Leem só {DADOS_BASE}/hotspots.geojson e as fontes da previsão (Meteomatics, Open-Meteo
e {DADOS_BASE}/forecast-snapshot.json), sempre com o id conferido contra a lista do
hotspots.geojson. A cadeia da previsão (previsao_do_hotspot) também serve a GET
/api/clima. Falhas voltam como dado ({"erro": ...}), para o Gemini se corrigir, em vez
de virar exceção. As fontes da resposta são montadas pelo código a
partir do campo "fonte" (ou "fontes") de cada resultado: {"nome", "data", "url"}.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from functools import wraps
from threading import Lock
from time import monotonic
from typing import Any, ParamSpec, TypeVar, get_args

import httpx
from pydantic import BaseModel, ValidationError
from strands import tool

from cass.config import Configuracao, carregar_config
from cass.formato import AVISO_DE_EXEMPLO, Idioma, formatar_data, formatar_dia
from cass.formato import formatar_fonte, formatar_numero
from cass.modelos import ClasseDominante, ColecaoHotspots, Hotspot, PrevisaoClima
from cass.modelos import PrevisaoSalva, RespostaMeteomatics, RespostaOpenMeteo

logger = logging.getLogger(__name__)

TIMEOUT_S = 5.0  # hotspots.geojson e forecast-snapshot.json
# Meteomatics e Open-Meteo: com as duas fora, o snapshot ainda chega antes dos 8 s
# que o app espera pela /api/clima.
TIMEOUT_AO_VIVO_S = 3.0
MAX_BYTES_DADOS = 1_048_576  # JSON dos seis hotspots e previsões; limite antes do parse.
CACHE_TTL_S = 300.0  # o hotspots.geojson só muda num deploy do frontend
CACHE_CLIMA_TTL_S = 600.0  # o forecast-snapshot.json
CACHE_PREVISAO_TTL_S = 1800.0  # por hotspot, qualquer que seja a fonte que serviu
DIAS_DA_PREVISAO = 7  # o que a cadeia busca; o previsao_tempo pede de 1 a 7
URL_METEOMATICS = "https://api.meteomatics.com"
URL_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
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
    valor = _validar(url, nome, modelo)
    _cache[url] = _Guardado(valor, monotonic() + ttl)
    return valor


def _validar(
    url: str, nome: str, modelo: type[M], auth: httpx.Auth | None = None,
    timeout: float = TIMEOUT_S,
) -> M:
    """GET com timeout, validado contra o modelo; qualquer falha vira _Recusa."""
    try:
        return modelo.model_validate_json(_baixar_json(url, nome, auth, timeout))
    except httpx.TimeoutException as e:
        raise _Recusa(erro=f"{nome} não respondeu em {timeout:g} s", url=url) from e
    except httpx.HTTPStatusError as e:
        raise _Recusa(erro=f"não consegui ler {nome} (HTTP {e.response.status_code})", url=url) from e
    except httpx.HTTPError as e:
        raise _Recusa(erro=f"não consegui ler {nome} ({type(e).__name__})", url=url) from e
    except ValidationError as e:
        raise _Recusa(
            erro=f"{nome} não segue o contrato de dados ({e.error_count()} erros)", url=url
        ) from e


def _baixar_json(
    url: str, nome: str, auth: httpx.Auth | None = None, timeout: float = TIMEOUT_S
) -> bytes:
    """Limita o corpo antes do JSON e evita descompressão sem limite em memória.

    O servidor deve respeitar Accept-Encoding: identity. Redirecionamentos não
    podem trocar o destino configurado. O prazo observado entre blocos limita
    fluxos que enviam bytes continuamente; uma leitura individual continua
    limitada pelo timeout HTTP (não é um prazo rígido de parede).
    """
    inicio = monotonic()
    with httpx.stream(
        "GET", url, timeout=timeout, follow_redirects=False,
        headers={"Accept-Encoding": "identity"}, auth=auth,
    ) as resposta:
        resposta.raise_for_status()
        if resposta.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
            raise _Recusa(erro=f"{nome} devolveu uma codificação não permitida", url=url)
        declarado = resposta.headers.get("content-length", "").lstrip("0")
        if declarado.isdecimal() and (
            len(declarado) > len(str(MAX_BYTES_DADOS)) or int(declarado) > MAX_BYTES_DADOS
        ):
            raise _Recusa(erro=f"{nome} excede o limite de {MAX_BYTES_DADOS} bytes", url=url)
        corpo = bytearray()
        for bloco in resposta.iter_bytes():
            if monotonic() - inicio >= timeout:
                raise httpx.ReadTimeout("prazo de leitura excedido", request=resposta.request)
            if len(bloco) > MAX_BYTES_DADOS - len(corpo):
                raise _Recusa(erro=f"{nome} excede o limite de {MAX_BYTES_DADOS} bytes", url=url)
            corpo.extend(bloco)
        return bytes(corpo)


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


def hotspots_por_id() -> dict[str, Hotspot] | None:
    """Hotspots do hotspots.geojson (mesmo cache das ferramentas); None se não der para ler."""
    try:
        colecao, _ = _colecao()
    except _Recusa:
        return None
    return {h.properties.id: h for h in colecao.features}


def ids_dos_hotspots() -> list[str] | None:
    """Ids do hotspots.geojson, na ordem do arquivo; None se não der para ler."""
    hotspots = hotspots_por_id()
    return None if hotspots is None else list(hotspots)


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
    """Consulta o que o radar mediu num hotspot: classe dominante, área afetada,
    confiança, data da última passagem e área de interesse.

    Valores null ainda não foram medidos. O efeito esperado do El Niño vem de
    contexto_enso.

    Args:
        id: id do hotspot, como "guaiba-rs" (os ids vêm de listar_hotspots).
    """
    hotspot, colecao, url = _hotspot(id)
    p = hotspot.properties
    return {
        "fonte": _fonte(FONTE_RADAR, p.last_pass, url),
        "dados_de_exemplo": colecao.metadata.sample,
        # mode="json": datas viram texto e os nulls continuam lá. O enso_effect fica
        # de fora: é contexto da NOAA CPC, não medição do NISAR (vem de contexto_enso).
        "hotspot": p.model_dump(mode="json", exclude={"enso_effect"}),
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
    referência e não a use para avisar de chuva. Se reserva for true, a previsão ao
    vivo falhou e esta é a última previsão salva: diga isso, com a data dela.

    Args:
        id: id do hotspot, como "guaiba-rs".
        dias: quantos dias, de 1 a 7.
    """
    if type(dias) is not int or not DIAS_MIN <= dias <= DIAS_MAX:
        raise _Recusa(erro=f"dias deve ser de {DIAS_MIN} a {DIAS_MAX}", validos=list(range(1, 8)))
    hotspot, _, _ = _hotspot(id)
    try:
        # A mesma função da GET /api/clima, sem passar por HTTP (e com o mesmo cache).
        obtida = previsao_do_hotspot(hotspot)
    except PrevisaoIndisponivel as falha:
        raise _Recusa(erro=f"nenhuma fonte de previsão serviu: {falha}") from falha
    previsao = obtida.previsao
    em_json = previsao.model_dump(mode="json")
    resultado = {
        "fonte": _fonte(FONTES_DE_PREVISAO[previsao.source], previsao.updated_at.date(), obtida.url),
        "hotspot_id": id,
        "atualizada_em": em_json["updated_at"],
        "so_referencia": _agora() - previsao.updated_at > PREVISAO_VELHA,
        "dias": em_json["days"][:dias],
    }
    if previsao.source == "snapshot":
        resultado |= {
            "reserva": True,
            "motivo_da_reserva": "; ".join(obtida.falhas),
            "dados_de_exemplo": obtida.dados_de_exemplo,
        }
    return resultado


# ---------- previsão: Meteomatics -> Open-Meteo -> forecast-snapshot.json ----------


@dataclass(frozen=True)
class PrevisaoObtida:
    previsao: PrevisaoClima  # o corpo da GET /api/clima (api-contract.md)
    url: str  # de onde veio, para a fonte do Cass
    dados_de_exemplo: bool = False  # metadata.sample do forecast-snapshot.json
    falhas: tuple[str, ...] = ()  # por que as fontes anteriores não serviram


class PrevisaoIndisponivel(Exception):
    """Nenhuma das fontes serviu; `falhas` traz o motivo de cada uma, na ordem."""

    def __init__(self, falhas: list[str]) -> None:
        super().__init__("; ".join(falhas))
        self.falhas = tuple(falhas)


_travas: dict[str, Lock] = {}
_trava_das_travas = Lock()


def _trava(chave: str) -> Lock:
    with _trava_das_travas:
        return _travas.setdefault(chave, Lock())


def previsao_do_hotspot(hotspot: Hotspot) -> PrevisaoObtida:
    """Chuva por dia no hotspot: Meteomatics (se houver credenciais), Open-Meteo e,
    por fim, a última previsão salva. As fontes ao vivo têm TIMEOUT_AO_VIVO_S e o
    snapshot TIMEOUT_S; o resultado fica em cache por 30 min por hotspot (e
    coordenadas), qualquer que seja a fonte.

    Um pedido que chega enquanto o mesmo hotspot está sendo buscado (pelo
    aquecimento, por exemplo) espera esse resultado em vez de consultar de novo.
    """
    lon, lat = hotspot.geometry.coordinates
    chave = f"previsao:{hotspot.properties.id}:{lat},{lon}"
    with _trava(chave):
        guardado = _cache.get(chave)
        if guardado is not None and monotonic() < guardado.validade:
            return guardado.valor
        return _buscar_previsao(hotspot, chave)


def _buscar_previsao(hotspot: Hotspot, chave: str) -> PrevisaoObtida:
    config = carregar_config()
    falhas: list[str] = []
    for fonte in _fontes_de_previsao(config):
        try:
            obtida = fonte(hotspot, config)
        except _Recusa as falha:
            falhas.append(falha.dados["erro"])
            continue
        obtida = replace(obtida, falhas=tuple(falhas))
        if falhas:
            logger.warning("previsão de %s pela reserva: %s", hotspot.properties.id, "; ".join(falhas))
        _cache[chave] = _Guardado(obtida, monotonic() + CACHE_PREVISAO_TTL_S)
        return obtida
    logger.warning("previsão de %s indisponível: %s", hotspot.properties.id, "; ".join(falhas))
    raise PrevisaoIndisponivel(falhas)


def aquecer_previsoes() -> dict[str, str]:
    """Busca, em paralelo, a previsão de cada hotspot do hotspots.geojson, para o
    primeiro pedido já achar o cache cheio. Roda na subida do serviço, em segundo
    plano. Devolve a fonte que serviu cada hotspot ("indisponível" se nenhuma);
    vazio se o hotspots.geojson não abrir.
    """
    hotspots = hotspots_por_id()
    if not hotspots:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(hotspots), 8), thread_name_prefix="aquecer") as pool:
        fontes = list(pool.map(_fonte_que_serviu, hotspots.values()))
    return dict(zip(hotspots, fontes, strict=True))


def _fonte_que_serviu(hotspot: Hotspot) -> str:
    try:
        return previsao_do_hotspot(hotspot).previsao.source
    except PrevisaoIndisponivel:
        return "indisponível"


_Fonte = Callable[[Hotspot, Configuracao], PrevisaoObtida]


def _fontes_de_previsao(config: Configuracao) -> list[_Fonte]:
    com_meteomatics = bool(config.meteomatics_user and config.meteomatics_password)
    return [*([_da_meteomatics] if com_meteomatics else []), _da_open_meteo, _do_snapshot]


def _da_meteomatics(hotspot: Hotspot, config: Configuracao) -> PrevisaoObtida:
    # precip_24h às 00:00Z de D+1 é a chuva do dia D (UTC): os intervalos da
    # Meteomatics terminam no horário pedido.
    lon, lat = hotspot.geometry.coordinates
    amanha = datetime.combine(_agora().date() + timedelta(days=1), datetime.min.time(), UTC)
    fim = amanha + timedelta(days=DIAS_DA_PREVISAO - 1)
    periodo = f"{amanha:%Y-%m-%dT%H:%M:%SZ}--{fim:%Y-%m-%dT%H:%M:%SZ}:P1D"
    url = f"{URL_METEOMATICS}/{periodo}/precip_24h:mm/{lat},{lon}/json"
    auth = httpx.BasicAuth(config.meteomatics_user, config.meteomatics_password)
    nome = "a Meteomatics"
    resposta = _validar(url, nome, RespostaMeteomatics, auth, TIMEOUT_AO_VIVO_S)
    dias = [
        _dia((v.date - timedelta(days=1)).date(), v.value)
        for v in resposta.data[0].coordinates[0].dates
    ]
    return _obtida(nome, "meteomatics", resposta.dateGenerated, dias, url)


def _da_open_meteo(hotspot: Hotspot, config: Configuracao) -> PrevisaoObtida:
    lon, lat = hotspot.geometry.coordinates
    url = str(httpx.URL(URL_OPEN_METEO, params={
        "latitude": lat, "longitude": lon, "daily": "precipitation_sum",
        "forecast_days": DIAS_DA_PREVISAO, "timezone": "GMT",
    }))
    nome = "a Open-Meteo"
    diario = _validar(url, nome, RespostaOpenMeteo, timeout=TIMEOUT_AO_VIVO_S).daily
    dias = []
    for dia, chuva in zip(diario.time, diario.precipitation_sum, strict=True):
        if chuva is None:
            break  # depois do primeiro dia sem modelo, a série acabou
        dias.append(_dia(dia, chuva))
    # A Open-Meteo não diz quando o modelo rodou: vale a hora da consulta.
    return _obtida(nome, "open-meteo", _agora(), dias, url)


def _do_snapshot(hotspot: Hotspot, config: Configuracao) -> PrevisaoObtida:
    url = f"{config.dados_base.rstrip('/')}/forecast-snapshot.json"
    nome = "o forecast-snapshot.json"
    salva = _ler(url, nome, PrevisaoSalva, CACHE_CLIMA_TTL_S)
    id = hotspot.properties.id
    if id not in salva.forecasts:
        raise _Recusa(erro=f"{nome} não tem o hotspot {id!r}", url=url)
    dias = [_dia(d.date, d.precipitation_mm) for d in salva.forecasts[id]]
    obtida = _obtida(nome, "snapshot", salva.updated_at, dias, url)
    return replace(obtida, dados_de_exemplo=salva.metadata.sample)


def _dia(dia: date, chuva: float) -> dict[str, Any]:
    return {"date": dia.isoformat(), "precipitation_mm": round(chuva, 1)}


def _obtida(
    nome: str, fonte: str, gerada: datetime, dias: list[dict[str, Any]], url: str
) -> PrevisaoObtida:
    """Monta o corpo da GET /api/clima; fora do contrato, passa para a próxima fonte."""
    try:
        previsao = PrevisaoClima.model_validate({
            "source": fonte,
            # Em UTC e sem microssegundos: o z.iso.datetime() do frontend só aceita "Z".
            "updated_at": gerada.astimezone(UTC).replace(microsecond=0),
            "days": dias[:DIAS_DA_PREVISAO],
        })
    except ValidationError as e:
        raise _Recusa(erro=f"{nome} não segue o contrato da /api/clima", url=url) from e
    return PrevisaoObtida(previsao=previsao, url=url)


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
