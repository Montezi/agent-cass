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
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    create_model,
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


# ---------- Resposta da GET /api/clima (docs/api-contract.md) ----------
# Espelho do zod forecastResponseSchema (frontend, src/shared/lib/forecastSchema.ts).


class DiaDePrevisao(_Contrato):
    date: DataAaaaMmDd
    precipitation_mm: Annotated[Numero, Field(ge=0)]


MAX_DIAS_DE_PREVISAO = 16  # api-contract.md: mais que isso conta como fora do contrato


class PrevisaoClima(_Contrato):
    source: Literal["meteomatics", "open-meteo", "snapshot"]
    updated_at: AwareDatetime
    days: list[DiaDePrevisao] = Field(min_length=1, max_length=MAX_DIAS_DE_PREVISAO)


class PrevisaoSalva(_Contrato):
    """{DADOS_BASE}/forecast-snapshot.json: espelho do zod forecastSnapshotSchema.

    É a reserva quando a /api/clima falha: uma lista de dias por id de hotspot.
    """

    metadata: Metadados = Field(default_factory=Metadados)
    source: Literal["snapshot"]
    updated_at: AwareDatetime
    forecasts: dict[str, Annotated[list[DiaDePrevisao], Field(min_length=1)]]


# ---------- Contrato público do POST /api/agente (docs/api-contract.md) ----------
# Espelho do zod agentAnswerSchema (frontend, src/features/cass/agentApi.ts).
# Chaves em inglês, como no contrato.


MAX_CARACTERES_PERGUNTA = 500


class PedidoAgente(BaseModel):
    # 500: api-contract.md do frontend (MAX_QUESTION_CHARS no agentApi.ts).
    question: str = Field(min_length=1, max_length=MAX_CARACTERES_PERGUNTA)
    # Gaveta aberta no app; o app sempre manda. Aqui só o formato do id: se ele está
    # no hotspots.geojson, o endpoint confere (cass/main.py, _conferir_hotspot).
    hotspot_id: Annotated[str, Field(pattern=PADRAO_ID, max_length=64)] | None = None
    language: Literal["pt", "en"]


# Limites do agentAnswerSchema (e do api-contract.md do frontend): uma resposta maior
# conta como fora do contrato e o app cai no modo demo.
MAX_PARAGRAFOS = 12
MAX_CARACTERES_PARAGRAFO = 2000
MAX_FERRAMENTAS = 10
MAX_CARACTERES_FERRAMENTA = 60
MAX_FONTES = 10
MAX_CARACTERES_FONTE = 200
MAX_CARACTERES_NOTA = 500


def tamanho_js(texto: str) -> int:
    """Tamanho como o zod e o JavaScript contam (String.length): unidades UTF-16.

    Um emoji fora do plano básico conta 2; len() do Python contaria 1.
    """
    return len(texto.encode("utf-16-le")) // 2


def _no_maximo(limite: int) -> AfterValidator:
    def validar(texto: str) -> str:
        if tamanho_js(texto) > limite:
            raise ValueError(f"no máximo {limite} caracteres")
        return texto

    return AfterValidator(validar)


class RespostaAgente(BaseModel):
    # **texto** marca o número medido em negrito; sem HTML (o app escapa).
    paragraphs: list[
        Annotated[str, Field(min_length=1), _no_maximo(MAX_CARACTERES_PARAGRAFO)]
    ] = Field(min_length=1, max_length=MAX_PARAGRAFOS)
    # Ferramentas chamadas, mostradas como chips; sem repetição.
    tools: list[Annotated[str, _no_maximo(MAX_CARACTERES_FERRAMENTA)]] = Field(
        max_length=MAX_FERRAMENTAS
    )
    # Fontes com data: "NISAR GCOV, 12 nov 2026".
    sources: list[Annotated[str, _no_maximo(MAX_CARACTERES_FONTE)]] = Field(
        max_length=MAX_FONTES
    )
    note: Annotated[str, _no_maximo(MAX_CARACTERES_NOTA)] | None = None  # aviso opcional

    @field_validator("tools")
    @classmethod
    def _ferramentas_sem_repeticao(cls, tools: list[str]) -> list[str]:
        if len(set(tools)) != len(tools):
            raise ValueError("ferramentas repetidas")
        return tools


# ---------- Formato interno do agente (apêndice A do plano) ----------
# Campos, tipos e limites iguais ao apêndice A. As descrições (Field description)
# repetem os comentários de lá: o Gemini lê o esquema da RespostaCass como ferramenta.
# cass/contrato.py converte a RespostaCass para a RespostaAgente.


class Fonte(BaseModel):
    nome: str = Field(description='Nome da fonte, como "NISAR GCOV", "Meteomatics", "WorldPop"')
    data: str = Field(description="Data do dado, não da pergunta")
    ferramenta: str = Field(description="Qual ferramenta devolveu o dado")


class Hipotese(BaseModel):
    texto: str
    evidencia: str
    forca: Literal["fraca", "moderada", "forte"]


class RespostaCass(BaseModel):
    """Resposta do Cass ao app."""

    tipo: Literal["resposta", "pergunta_de_volta", "fora_do_escopo", "erro"] = Field(
        description=(
            "resposta: respondeu com dados das ferramentas; pergunta_de_volta: o hotspot "
            "não está claro, pergunte e preencha opcoes; fora_do_escopo: a mensagem não é "
            "sobre os hotspots, o radar, o clima, o El Niño ou este app"
        )
    )
    idioma: Literal["pt", "en"]
    texto: str = Field(
        max_length=900,
        description="A resposta, em parágrafos separados por linha em branco; o que foi "
        "medido vai em medido e a hipótese em hipotese, sem repetir aqui",
    )
    medido: list[str] = Field(
        default=[],
        description="Frases com número, unidade e data, só do que foi medido; "
        "marque o número com **, como **212 km²**",
    )
    hipotese: Hipotese | None = None
    fontes: list[Fonte] = []
    confianca: Literal["alta", "media", "baixa"]
    opcoes: list[str] = Field(default=[], description="Botões quando o Cass pergunta de volta")
    sugestoes: list[str] = Field(default=[], description="Próximas perguntas")
    dados_de_exemplo: bool = Field(default=False, description="true quando metadata.sample = true")


# O que o modelo preenche: a RespostaCass sem os campos que o código sabe (idioma do
# pedido, fontes e dados_de_exemplo tirados dos resultados das ferramentas). Mesmos
# tipos, limites e descrições; o agente completa a RespostaCass depois.
CAMPOS_DO_CODIGO = frozenset({"idioma", "fontes", "dados_de_exemplo"})
RespostaDoModelo = create_model(
    "RespostaDoModelo",
    __doc__="Resposta do Cass ao app.",
    **{
        nome: (campo.annotation, campo)
        for nome, campo in RespostaCass.model_fields.items()
        if nome not in CAMPOS_DO_CODIGO
    },
)
