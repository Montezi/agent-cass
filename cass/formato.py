"""Datas, números e fontes em texto, no mesmo formato do frontend (src/shared/lib/format.ts)."""

import re
from datetime import date
from typing import Literal

Idioma = Literal["pt", "en"]

MESES: dict[Idioma, list[str]] = {
    "pt": ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}
SEM_DATA: dict[Idioma, str] = {"pt": "data não informada", "en": "date not provided"}
AVISO_DE_EXEMPLO: dict[Idioma, str] = {
    "pt": "Dados de exemplo: posições aproximadas e valores ilustrativos.",
    "en": "Sample data: approximate positions and illustrative values.",
}
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(T.*)?")


def _como_data(texto: str) -> date | None:
    if not _ISO.fullmatch(texto):
        return None
    try:
        return date.fromisoformat(texto[:10])
    except ValueError:
        return None


def formatar_data(texto: str, idioma: Idioma) -> str:
    """"2026-11-12" -> "12 nov 2026" / "Nov 12, 2026". O que não for data volta como veio."""
    d = _como_data(texto)
    if d is None:
        return texto
    mes = MESES[idioma][d.month - 1]
    return f"{d.day} {mes} {d.year}" if idioma == "pt" else f"{mes} {d.day}, {d.year}"


def formatar_dia(texto: str, idioma: Idioma) -> str:
    """"2026-11-15" -> "15 nov" / "Nov 15"."""
    d = _como_data(texto)
    if d is None:
        return texto
    mes = MESES[idioma][d.month - 1]
    return f"{d.day} {mes}" if idioma == "pt" else f"{mes} {d.day}"


def formatar_numero(valor: float, idioma: Idioma) -> str:
    """212.5 -> "212,5" / "212.5"; 18.0 -> "18". Até duas casas, sem zeros à direita."""
    texto = f"{valor:.2f}".rstrip("0").rstrip(".")
    return texto.replace(".", ",") if idioma == "pt" else texto


def formatar_fonte(nome: str, data: str | None, idioma: Idioma) -> str:
    """"NISAR, 12 nov 2026"; sem data, "NISAR, data não informada"."""
    data = (data or "").strip()
    return f"{nome}, {formatar_data(data, idioma) if data else SEM_DATA[idioma]}"
