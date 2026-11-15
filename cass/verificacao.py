"""Confere a saída com os dados usados, antes de convertê-la para o contrato público."""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from cass.formato import Idioma, formatar_numero
from cass.modelos import RespostaCass

logger = logging.getLogger(__name__)

Resultado = tuple[str, dict[str, Any]]
MAX_CARACTERES_VALOR = 40  # o valor vai para o log e para a mensagem de correção
MAX_VALORES_NO_LOG = 10

_IGNORAR = frozenset({
    "id", "hotspot_id", "validos", "toolUseId", "url", "uri", "link",
    "path", "caminho", "layers", "bbox", "coordinates", "geometry",
    "metadata", "sample", "dados_de_exemplo", "_tipo", "status", "type",
    "name", "short_name", "nome", "region", "kind", "dominant_class",
    "classe", "strength", "forca", "confianca", "language", "idioma",
    "ferramenta", "motivo", "erro",
})
_FONTES = frozenset({"fonte", "fontes", "source", "sources", "provenance"})
_CAMPOS_DATA = frozenset({
    "date", "data", "last_pass", "baseline_pass", "updated_at", "atualizada_em",
    "baseline_date", "radar_through",
})
_TEXTOS_COM_DADOS = frozenset({
    "texto", "text", "evidencia", "evidence", "value", "valor", "note", "nota",
    "rascunho", "medido", "measured",
})
_MESES = {
    "jan": 1, "janeiro": 1, "january": 1,
    "fev": 2, "fevereiro": 2, "feb": 2, "february": 2,
    "mar": 3, "março": 3, "marco": 3, "march": 3,
    "abr": 4, "abril": 4, "apr": 4, "april": 4,
    "mai": 5, "maio": 5, "may": 5,
    "jun": 6, "junho": 6, "june": 6,
    "jul": 7, "julho": 7, "july": 7,
    "ago": 8, "agosto": 8, "aug": 8, "august": 8,
    "set": 9, "setembro": 9, "sep": 9, "sept": 9, "september": 9,
    "out": 10, "outubro": 10, "oct": 10, "october": 10,
    "nov": 11, "novembro": 11, "november": 11,
    "dez": 12, "dezembro": 12, "dec": 12, "december": 12,
}
_MES = "|".join(sorted(_MESES, key=len, reverse=True))
_DATA_ISO = re.compile(
    r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?"
    r"(?![0-9])"
)
_DATA_BARRAS = re.compile(r"(?<![0-9])([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})(?![0-9])")
_DATA_PT = re.compile(
    rf"\b(?P<dia>[0-9]{{1,2}})\s+(?:de\s+)?(?P<mes>{_MES})\.?"
    r"(?:\s+(?:de\s+)?(?P<ano>[0-9]{4}))?\b",
    re.IGNORECASE,
)
_DATA_EN = re.compile(
    rf"\b(?P<mes>{_MES})\.?\s+(?P<dia>[0-9]{{1,2}})"
    r"(?:,?\s+(?P<ano>[0-9]{4}))?\b",
    re.IGNORECASE,
)
_ANO_EM_CONTEXTO = re.compile(
    r"\b(?:em|desde|ate|até|entre|de|in|since|until|from|of|during)\s+([0-9]{4})\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
# [0-9], em vez de \d: o expoente de km² não é uma medição de 2.
_NUMERO = re.compile(
    r"(?<![\w])[-+−]?[0-9]+(?:[.,][0-9]+)*"
    r"(?:[ \u00a0\u202f][0-9]{3})*(?:[.,][0-9]+)?(?:[eE][+-]?[0-9]+)?"
)
_PERCENTUAL = re.compile(r"\s*(?:\*{1,2})?\s*(?:%|por\s+cento\b|percent\b)", re.IGNORECASE)
_UNIDADE = re.compile(r"\b(?:km|cm|mm|m)(?:\^|\*\*)[23]\b", re.IGNORECASE)
_MEDIDA_APOS_ANO = re.compile(
    r"\s*\*{0,2}\s*(?:%|km\b|km[²2]|m[²2]|mm\b|cm\b|ha\b|pessoas\b|people\b|dias\b|days\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NaoConfirmado:
    """Um número ou data da resposta sem apoio nos resultados das ferramentas."""

    campo: str  # "texto", "medido[0]", "opcoes[1]", "hipotese.texto", "hipotese.evidencia"
    valor: str  # o trecho como o modelo escreveu, como "2024", "88%" ou "13 nov 2026"


@dataclass(frozen=True)
class Verificacao:
    """Resultado da camada final. Verdadeiro quando a resposta pode ser publicada."""

    nao_confirmados: tuple[NaoConfirmado, ...] = ()
    causais: tuple[str, ...] = ()  # campos que afirmam que o El Niño causou a mudança

    def __bool__(self) -> bool:
        return not self.nao_confirmados and not self.causais


@dataclass
class _Evidencias:
    numeros: set[Decimal] = field(default_factory=set)
    percentuais: set[Decimal] = field(default_factory=set)
    datas: set[date] = field(default_factory=set)
    dias_meses: set[tuple[int, int]] = field(default_factory=set)
    instantes: set[str] = field(default_factory=set)
    anos: set[int] = field(default_factory=set)
    nomes_fontes: set[str] = field(default_factory=set)


def _numero_localizado(texto: str, idioma: Idioma) -> Decimal | None:
    """Não interpreta um agrupamento inválido como outro número menor."""
    texto = texto.replace("−", "-")
    partes = re.split(r"[eE]", texto)
    if len(partes) > 2:
        return None
    mantissa = partes[0]
    expoente = f"e{partes[1]}" if len(partes) == 2 else ""
    decimal, milhar = (",", ".") if idioma == "pt" else (".", ",")
    if idioma == "pt" and re.fullmatch(r"[-+]?0\.[0-9]+", mantissa):
        # "0.86": em PT, nenhum milhar começa com 0, então o ponto só pode ser decimal.
        decimal, milhar = ".", ","
    if mantissa.count(decimal) > 1:
        return None
    inteiro, separador, fracao = mantissa.partition(decimal)
    sinal = inteiro[:1] if inteiro.startswith(("-", "+")) else ""
    inteiro = inteiro[len(sinal):]
    if separador and not re.fullmatch(r"[0-9]+", fracao):
        return None
    if re.search(r"[ \u00a0\u202f]", inteiro):
        if milhar in inteiro or not re.fullmatch(r"[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+", inteiro):
            return None
        inteiro = re.sub(r"[ \u00a0\u202f]", "", inteiro)
    elif milhar in inteiro:
        if not re.fullmatch(rf"[0-9]{{1,3}}(?:{re.escape(milhar)}[0-9]{{3}})+", inteiro):
            return None
        inteiro = inteiro.replace(milhar, "")
    elif not re.fullmatch(r"[0-9]+", inteiro):
        return None
    try:
        numero = Decimal(sinal + inteiro + ("." + fracao if separador else "") + expoente)
        return numero if numero.is_finite() else None
    except InvalidOperation:
        return None


def _data_iso(texto: str) -> date | None:
    if not _DATA_ISO.fullmatch(texto):
        return None
    try:
        return date.fromisoformat(texto[:10])
    except ValueError:
        return None


def _fonte(valor: Any, evidencias: _Evidencias) -> None:
    if isinstance(valor, list):
        for item in valor:
            _fonte(item, evidencias)
    elif isinstance(valor, dict):
        nome = valor.get("nome") or valor.get("name")
        if isinstance(nome, str):
            evidencias.nomes_fontes.add(nome)
        for chave, dado in valor.items():
            if chave in _CAMPOS_DATA and isinstance(dado, str):
                _registrar_data(dado, evidencias)


def _registrar_data(texto: str, evidencias: _Evidencias) -> None:
    d = _data_iso(texto)
    if d is not None:
        evidencias.datas.add(d)
        evidencias.dias_meses.add((d.day, d.month))
        evidencias.anos.add(d.year)
        if "T" in texto:
            evidencias.instantes.add(texto)


def _coletar(
    valor: Any,
    evidencias: _Evidencias,
    idioma: Idioma,
    campo: str = "",
    texto_de_dado: bool = False,
) -> None:
    if isinstance(valor, dict):
        if "erro" in valor or valor.get("disponivel") is False or valor.get("status") == "error":
            return
        for chave, dado in valor.items():
            if chave in _FONTES:
                _fonte(dado, evidencias)
        for chave, dado in valor.items():
            if chave in _FONTES:
                continue
            elif chave in _CAMPOS_DATA and isinstance(dado, str):
                _registrar_data(dado, evidencias)
            elif chave not in _IGNORAR:
                lingua = chave if chave in {"pt", "en"} else idioma
                _coletar(dado, evidencias, lingua, chave, texto_de_dado or chave in _TEXTOS_COM_DADOS)
    elif isinstance(valor, list):
        for item in valor:
            _coletar(item, evidencias, idioma, campo, texto_de_dado)
    elif isinstance(valor, (int, float, Decimal)) and not isinstance(valor, bool):
        numero = Decimal(str(valor))
        if numero.is_finite():
            evidencias.numeros.add(numero)
            evidencias.numeros.add(Decimal(formatar_numero(valor, "en")))
            if campo == "confidence" and 0 <= numero <= 1:
                evidencias.percentuais.update({numero * 100, Decimal(round(valor * 100))})
            elif campo.endswith("_pct") or campo in {"pct", "percentual", "percentage"}:
                evidencias.percentuais.add(numero)
    elif isinstance(valor, str) and texto_de_dado:
        _coletar_texto(valor, evidencias, idioma)


def _sem_proveniencia(texto: str, evidencias: _Evidencias) -> str:
    texto = _URL.sub(lambda m: " " * len(m[0]), texto)
    texto = _UNIDADE.sub(lambda m: " " * len(m[0]), texto)
    for nome in sorted(evidencias.nomes_fontes, key=len, reverse=True):
        # Rótulos como Sentinel-1 podem ser citados. Uma "fonte" que seja uma
        # quantidade, porém, nunca pode esconder uma medição sem evidência.
        quantidade = re.search(
            r"[0-9]\s*(?:%|km[²2]?\b|m[²2]\b|mm\b|cm\b|ha\b|pessoas\b|people\b|dias\b|days\b)",
            nome, re.IGNORECASE,
        )
        if nome and re.search(r"[A-Za-z]", nome) and not quantidade:
            texto = re.sub(re.escape(nome), lambda m: " " * len(m[0]), texto, flags=re.IGNORECASE)
    return texto


def _datas_no_texto(texto: str, idioma: Idioma) -> list[tuple[int, int, int | None, int | None, int | None, str]]:
    """Spans de datas completos ou das formas de exibição já usadas pelo Cass."""
    datas: list[tuple[int, int, int | None, int | None, int | None, str]] = []

    def ocupado(inicio: int, fim: int) -> bool:
        return any(inicio < b and fim > a for a, b, *_ in datas)

    for m in _DATA_ISO.finditer(texto):
        d = _data_iso(m[0])
        datas.append((m.start(), m.end(), d.year if d else 0, d.month if d else 0, d.day if d else 0, m[0]))
    for m in _DATA_BARRAS.finditer(texto):
        if not ocupado(m.start(), m.end()):
            primeiro, segundo, ano = map(int, m.groups())
            dia, mes = (primeiro, segundo) if idioma == "pt" else (segundo, primeiro)
            datas.append((m.start(), m.end(), ano, mes, dia, m[0]))
    for padrao in (_DATA_PT, _DATA_EN):
        for m in padrao.finditer(texto):
            if not ocupado(m.start(), m.end()):
                ano = int(m["ano"]) if m["ano"] else None
                datas.append((m.start(), m.end(), ano, _MESES[m["mes"].lower()], int(m["dia"]), m[0]))
    for m in _ANO_EM_CONTEXTO.finditer(texto):
        inicio, fim = m.span(1)
        if not ocupado(inicio, fim) and not _MEDIDA_APOS_ANO.match(texto[fim:]):
            datas.append((inicio, fim, int(m[1]), None, None, m[1]))
    return datas


def _mascarar(texto: str, spans: list[tuple]) -> str:
    caracteres = list(texto)
    for inicio, fim, *_ in spans:
        caracteres[inicio:fim] = " " * (fim - inicio)
    return "".join(caracteres)


def _coletar_texto(texto: str, evidencias: _Evidencias, idioma: Idioma) -> None:
    # O rascunho é montado pelo código; fontes e links dentro dele não são medições.
    texto = re.sub(
        r"(?im)^\s*(?:fonte|fontes|source|sources|veja no app|see it in the app|"
        r"alerta cassaandra|cassaandra alert):[^\n]*", "", texto,
    )
    texto = _sem_proveniencia(texto, evidencias)
    datas = _datas_no_texto(texto, idioma)
    for _, _, ano, mes, dia, literal in datas:
        if ano and mes and dia:
            try:
                evidencias.datas.add(date(ano, mes, dia))
                evidencias.dias_meses.add((dia, mes))
                evidencias.anos.add(ano)
                if "T" in literal:
                    evidencias.instantes.add(literal)
            except ValueError:
                pass
        elif ano and mes is None:
            evidencias.anos.add(ano)
        elif mes and dia:
            try:
                date(2000, mes, dia)
                evidencias.dias_meses.add((dia, mes))
            except ValueError:
                pass
    texto = _mascarar(texto, datas)
    for m in _NUMERO.finditer(texto):
        numero = _numero_localizado(m[0], idioma)
        if numero is not None:
            if _PERCENTUAL.match(texto[m.end():]):
                evidencias.percentuais.add(numero)
            else:
                evidencias.numeros.add(numero)


def _textos_publicados(resposta: RespostaCass) -> list[tuple[str, str]]:
    """(campo, texto) de todos os campos livres que para_contrato transforma em parágrafos."""
    textos = [("texto", resposta.texto)]
    textos += [(f"medido[{i}]", t) for i, t in enumerate(resposta.medido)]
    textos += [(f"opcoes[{i}]", t) for i, t in enumerate(resposta.opcoes)]
    if resposta.hipotese is not None:
        textos += [("hipotese.texto", resposta.hipotese.texto), ("hipotese.evidencia", resposta.hipotese.evidencia)]
    return textos


def _nomes_de_hotspots(resultados: list[Resultado]) -> set[str]:
    """Nomes exatos podem conter dígitos, mas não autorizam medições com esses dígitos."""
    nomes: set[str] = set()
    for _, dados in resultados:
        if "erro" in dados or dados.get("disponivel") is False:
            continue
        hotspots = [dados.get("hotspot"), *(dados.get("hotspots") or [])]
        for hotspot in hotspots:
            if not isinstance(hotspot, dict):
                continue
            for chave in ("name", "short_name"):
                nome = hotspot.get(chave)
                if isinstance(nome, dict):
                    nomes.update(v.strip() for v in nome.values() if isinstance(v, str))
    return nomes


def valores_nao_confirmados(resposta: RespostaCass, resultados: list[Resultado]) -> list[NaoConfirmado]:
    """Números e datas de todos os campos publicados que os resultados não confirmam.

    Valores ausentes nunca viram zero. A comparação reconhece o formato PT/EN, datas
    do dado e confiança em %. Não usa IDs, coordenadas ou fontes como prova de uma
    medição e não calcula novas estatísticas nem infere valores a partir do número
    de itens de uma lista. Cada (campo, valor) aparece uma vez, na ordem do texto.
    """
    evidencias = _Evidencias()
    for _, dados in resultados:
        _coletar(dados, evidencias, resposta.idioma)
    nomes = _nomes_de_hotspots(resultados)
    encontrados: dict[NaoConfirmado, None] = {}
    for campo, trecho in _textos_publicados(resposta):
        if campo.startswith("opcoes[") and trecho.strip() in nomes:
            continue
        texto = _sem_proveniencia(trecho, evidencias)
        datas = _datas_no_texto(texto, resposta.idioma)
        for _, _, ano, mes, dia, literal in datas:
            if "T" in literal:
                confirmada = literal in evidencias.instantes
            elif mes is None:
                confirmada = ano in evidencias.anos
            elif ano is None:
                confirmada = (dia, mes) in evidencias.dias_meses
            else:
                confirmada = any(d.day == dia and d.month == mes and (ano is None or d.year == ano) for d in evidencias.datas)
            if not confirmada:
                encontrados[_nao_confirmado(campo, literal)] = None
        texto = _mascarar(texto, datas)
        for m in _NUMERO.finditer(texto):
            numero = _numero_localizado(m[0], resposta.idioma)
            percentual = _PERCENTUAL.match(texto[m.end():])
            permitidos = evidencias.percentuais if percentual else evidencias.numeros
            if numero is None or numero not in permitidos:
                encontrados[_nao_confirmado(campo, m[0] + ("%" if percentual else ""))] = None
    return list(encontrados)


def _nao_confirmado(campo: str, literal: str) -> NaoConfirmado:
    """O valor numa linha só e curto: ele vai para o log e de volta ao modelo."""
    valor = " ".join(literal.split())
    if len(valor) > MAX_CARACTERES_VALOR:
        valor = valor[: MAX_CARACTERES_VALOR - 1] + "…"
    return NaoConfirmado(campo=campo, valor=valor)


def numeros_confirmados(resposta: RespostaCass, resultados: list[Resultado]) -> bool:
    """Todo número e data dos campos publicados está nos resultados das ferramentas?"""
    return not valores_nao_confirmados(resposta, resultados)


_EL_NINO = r"\bel\s+nino\b"
_VERBO_CAUSAL = (
    r"(?:caus(?:ou|a|aram|ado|ada|ados|adas|ed|es|e)|provoc(?:ou|a|ado|ada)|"
    r"ger(?:ou|a|ado|ada)|desencade(?:ou|ia|ado|ada)|trigger(?:ed|s)?|"
    r"led\s+to|leads?\s+to|levou\s+a|result(?:ou\s+em|ed\s+in|s\s+in)|"
    r"responsavel\s+(?:por|pelo|pela|pelos|pelas)|responsible\s+for|a\s+causa\s+d[ao]|the\s+cause\s+of)"
)
_AUXILIARES = (
    r"(?:(?:nao|nunca|jamais|talvez|possivelmente|provavelmente|not|never|may|might|"
    r"could|can|did|does|has|had|have|pode|podem|poderia|ter|tenha|teria|foi|e|is|"
    r"was|be|been|ser|directly|diretamente|certamente|definitivamente|certainly|"
    r"definitely|so|apenas|only|que|o|a|the)\s+){0,8}"
)
_CAUSA_ATIVA = re.compile(_EL_NINO + r"\s+" + _AUXILIARES + _VERBO_CAUSAL + r"\b")
_CAUSA_PASSIVA = re.compile(_VERBO_CAUSAL + r"\s+(?:pelo|pela|por|by)\s+" + _EL_NINO)
_CAUSA_NOMINAL = re.compile(r"\b(?:por\s+causa\s+d[oe]|devido\s+ao|due\s+to|because\s+of)\s+" + _EL_NINO)
_INCERTEZA = re.compile(
    r"\b(?:nao|nunca|jamais|sem\s+(?:evidencia|prova)|no\s+(?:evidence|proof)|not|never|cannot|"
    r"can['’]t|didn['’]t|doesn['’]t|isn['’]t|wasn['’]t|hypothes[ie]s|hipotese|"
    r"possivel|possivelmente|talvez|pode|poderia|podem|may|might|could|possible|"
    r"potential|suspected)\b"
)
_CERTEZA_NEGADA = re.compile(
    r"\b(?:nao\s+(?:ha|existe[m]?|temos|tenho)\s+(?:nenhuma\s+)?duvidas?|"
    r"nao\s+(?:so|apenas)|not\s+only|nao\s+e\s+(?:uma\s+)?hipotese|"
    r"not\s+(?:a\s+)?hypothesis)\b"
)
_RESSALVA_APOS = re.compile(
    r"^\s+(?:(?:is|e|continua|remains)\s+)?"
    r"(?:(?:only|apenas|somente|ainda|still)\s+)?"
    r"(?:(?:uma?|an?)\s+)?(?:hipotese|hypothesis|incerto|uncertain)\b"
)


def afirma_causalidade(texto: str) -> bool:
    """Reconhece afirmações causais explícitas PT/EN, preservando incerteza e perguntas."""
    normalizado = "".join(
        c for c in unicodedata.normalize("NFKD", texto.lower()) if not unicodedata.combining(c)
    )
    for frase in re.findall(r"[^.!?;\n]+[.!?;]?", normalizado):
        if frase.rstrip().endswith("?"):
            continue
        # Uma ressalva numa oração não autoriza uma afirmação depois de "mas".
        for parte in re.split(
            r"\b(?:mas|porem|contudo|however|but|portanto|therefore|thus)\b|"
            r"\bso\s+(?=(?:(?:the)\s+)?el\s+nino\b)|"
            r"\b(?:por\s+isso|as\s+a\s+result)\b|"
            r",\s*(?=(?:(?:e|and)\s+)?(?:(?:o|the)\s+)?el\s+nino\b)|"
            r"\s+(?:e|and)\s+(?=(?:(?:o|the)\s+)?el\s+nino\b)",
            frase,
        ):
            parte = parte.replace(",", " ")
            for padrao in (_CAUSA_ATIVA, _CAUSA_PASSIVA, _CAUSA_NOMINAL):
                for m in padrao.finditer(parte):
                    contexto = parte[:m.end()]
                    contexto = _CERTEZA_NEGADA.sub("", contexto)
                    ressalva = _RESSALVA_APOS.match(parte[m.end():])
                    if not _INCERTEZA.search(contexto) and not ressalva:
                        return True
    return False


def validar_resposta(resposta: RespostaCass, resultados: list[Resultado]) -> Verificacao:
    """Camada final antes de devolver a resposta: números confirmados e sem causa afirmada.

    Uma recusa vai para o log com o campo e o valor, nunca com o texto da resposta.
    """
    verificacao = Verificacao(
        nao_confirmados=tuple(valores_nao_confirmados(resposta, resultados)),
        causais=tuple(campo for campo, texto in _textos_publicados(resposta) if afirma_causalidade(texto)),
    )
    if not verificacao:
        _registrar_recusa(verificacao)
    return verificacao


def _registrar_recusa(verificacao: Verificacao) -> None:
    valores = [f"{n.campo}={n.valor!r}" for n in verificacao.nao_confirmados[:MAX_VALORES_NO_LOG]]
    sobra = len(verificacao.nao_confirmados) - MAX_VALORES_NO_LOG
    if sobra > 0:
        valores.append(f"e mais {sobra}")
    logger.warning(
        "verificação recusou a resposta; não confirmados: %s; causa afirmada em: %s",
        ", ".join(valores) or "nenhum", ", ".join(verificacao.causais) or "nenhum campo",
    )


def resposta_segura(resposta: RespostaCass) -> RespostaCass:
    """Retira afirmações não confirmadas, preservando somente proveniência e aviso de exemplo."""
    textos: dict[Idioma, str] = {
        "pt": "Não consegui confirmar os detalhes nos dados disponíveis. Consulte as fontes e tente uma pergunta mais específica.",
        "en": "I could not confirm the details in the available data. Check the sources and try a more specific question.",
    }
    return RespostaCass(
        tipo="resposta",
        idioma=resposta.idioma,
        texto=textos[resposta.idioma],
        confianca="baixa",
        fontes=[fonte.model_copy(deep=True) for fonte in resposta.fontes],
        dados_de_exemplo=resposta.dados_de_exemplo,
    )
