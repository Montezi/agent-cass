"""Espelho Pydantic do contrato de dados do hotspots.geojson.

Fontes: docs/data-contract.md e o schema zod do frontend
(src/shared/lib/hotspotSchema.ts e data.ts). Mude os três juntos.
As chaves seguem o contrato, em inglês.
"""

import re
from collections import Counter
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    field_validator,
    model_validator,
)

ClasseDominante = Literal["flood", "drought", "fire_deforestation", "deformation", "stable"]

PADRAO_ID = r"^[a-z0-9]+(-[a-z0-9]+)*$"
_DATA = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Igual ao dataPathSchema do frontend: segmentos de letras, dígitos, "_", "-" e "."
_CAMINHO = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")

# Como o z.number(): aceita int e float, recusa texto ("0.87") e booleano.
Numero = Annotated[float, Strict()]
Longitude = Annotated[Numero, Field(ge=-180, le=180)]
Latitude = Annotated[Numero, Field(ge=-90, le=90)]


def _exigir_aaaa_mm_dd(valor: object) -> object:
    """Só aceita o texto AAAA-MM-DD; a data em si o Pydantic confere depois."""
    if not (isinstance(valor, str) and _DATA.fullmatch(valor)):
        raise ValueError("a data deve estar no formato AAAA-MM-DD, como 2026-11-12")
    return valor


def _validar_caminho_png(caminho: str) -> str:
    """Caminho de camada relativo a /dados/, como layers/guaiba-rs/flood.png."""
    if "://" in caminho:
        raise ValueError("o caminho não pode ser uma URL; use um caminho relativo a /dados/")
    if "?" in caminho or "#" in caminho:
        raise ValueError("o caminho não pode ter query string nem #; a versão quem põe é o app")
    if caminho.startswith("/"):
        raise ValueError("o caminho é relativo a /dados/: sem / inicial e sem /dados/")
    if ".." in caminho:
        raise ValueError("o caminho não pode ter ..")
    if not _CAMINHO.fullmatch(caminho):
        raise ValueError(
            'use só letras, dígitos, _, -, . e /, como "layers/guaiba-rs/flood.png"'
        )
    if not caminho.lower().endswith(".png"):
        raise ValueError("a camada deve ser um arquivo .png")
    return caminho


DataAaaaMmDd = Annotated[date, BeforeValidator(_exigir_aaaa_mm_dd)]
CaminhoPng = Annotated[str, AfterValidator(_validar_caminho_png)]


class _Contrato(BaseModel):
    # Campos extras são permitidos e ignorados: o pipeline pode adiantar campos.
    model_config = ConfigDict(extra="ignore")


class Nome(_Contrato):
    """Texto mostrado ao usuário, nas duas línguas do app."""

    pt: Annotated[str, Field(min_length=1)]
    en: Annotated[str, Field(min_length=1)]


class Propriedades(_Contrato):
    id: Annotated[str, Field(pattern=PADRAO_ID)]
    name: Nome
    short_name: Nome | None = None
    # Contexto, não medição. Anulável, mas a chave é obrigatória.
    enso_effect: Nome | None
    # Valores medidos: null até o pipeline calcular. Nunca inventar número.
    dominant_class: ClasseDominante | None
    affected_area_km2: Annotated[Numero, Field(ge=0)] | None
    confidence: Annotated[Numero, Field(ge=0, le=1)] | None
    last_pass: DataAaaaMmDd | None
    # [west, south, east, north]; nenhum hotspot cruza o meridiano de 180°.
    bbox: tuple[Longitude, Latitude, Longitude, Latitude]
    # Nome da lente -> PNG relativo a /dados/.
    layers: dict[str, CaminhoPng]

    @field_validator("bbox")
    @classmethod
    def _bbox_ordenado(
        cls, bbox: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        oeste, sul, leste, norte = bbox
        if not (oeste < leste and sul < norte):
            raise ValueError(
                "bbox deve ser [west, south, east, north] com west < east e south < north"
            )
        return bbox


class Ponto(_Contrato):
    type: Literal["Point"]
    # [longitude, latitude], graus WGS 84.
    coordinates: tuple[Longitude, Latitude]


class Hotspot(_Contrato):
    """Uma Feature GeoJSON com um Point."""

    type: Literal["Feature"]
    geometry: Ponto
    properties: Propriedades

    @model_validator(mode="after")
    def _ponto_dentro_do_bbox(self) -> Self:
        lon, lat = self.geometry.coordinates
        oeste, sul, leste, norte = self.properties.bbox
        if not (oeste <= lon <= leste and sul <= lat <= norte):
            raise ValueError(
                f"o ponto [{lon}, {lat}] de {self.properties.id} está fora do bbox "
                f"{list(self.properties.bbox)}"
            )
        return self


class Metadados(_Contrato):
    # true: dados feitos à mão, com posições aproximadas; o Cass precisa avisar.
    sample: Annotated[bool, Strict()] = False


class ColecaoHotspots(_Contrato):
    """O hotspots.geojson inteiro: uma FeatureCollection."""

    type: Literal["FeatureCollection"]
    metadata: Metadados = Field(default_factory=Metadados)
    features: list[Hotspot]

    @model_validator(mode="after")
    def _ids_unicos(self) -> Self:
        contagem = Counter(h.properties.id for h in self.features)
        repetidos = sorted(id_ for id_, n in contagem.items() if n > 1)
        if repetidos:
            raise ValueError(f"ids de hotspot repetidos: {', '.join(repetidos)}")
        return self
