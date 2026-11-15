"""Confirmação de números e causalidade com dados locais, sem modelo ou rede."""

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from cass.modelos import Fonte, Hipotese, RespostaCass
from cass.verificacao import (
    NaoConfirmado,
    afirma_causalidade,
    numeros_confirmados,
    resposta_segura,
    validar_resposta,
    valores_nao_confirmados,
)

FIXTURE = Path(__file__).parent / "fixtures" / "hotspots.geojson"


def resposta(
    texto: str = "O radar ainda não tem medição.",
    idioma: str = "pt",
    medido: list[str] | None = None,
    hipotese: Hipotese | None = None,
) -> RespostaCass:
    return RespostaCass(
        tipo="resposta", idioma=idioma, texto=texto, medido=medido or [],
        hipotese=hipotese, confianca="alta",
    )


def dados(valor: Any) -> list[tuple[str, dict]]:
    return [("consultar_hotspot", valor)]


@pytest.mark.parametrize(
    ("idioma", "texto", "valor"),
    [
        ("pt", "Área de **2.500,25 km²**.", 2500.25),
        ("en", "An area of **2,500.25 km²**.", 2500.25),
        ("pt", "Área de **2.500 km²**.", 2500),
        ("en", "An area of **2,500 km²**.", 2500),
        ("pt", "Área de **2 500,25 km²**.", 2500.25),
        ("en", "An area of **2\u202f500.25 km²**.", 2500.25),
        ("pt", "Área de **212,56 km²**.", 212.555),
        ("en", "A change of **−2.2 dB**.", -2.2),
        ("en", "An area of **1e3 km²**.", 1000),
    ],
)
def test_numeros_medidos_com_localizacao_e_arredondamento_do_app(
    idioma: str, texto: str, valor: float,
) -> None:
    assert numeros_confirmados(resposta(texto, idioma), dados({"affected_area_km2": valor}))


@pytest.mark.parametrize(
    ("idioma", "texto"),
    [
        ("pt", "Área de 2.50,25 km²."),
        ("en", "An area of 2,50.25 km²."),
        ("pt", "Área de 2.500.25 km²."),
        ("en", "An area of 2,500,25 km²."),
        ("pt", "Área de 2,5 km²."),
        ("en", "An area of 2.5 km²."),
    ],
)
def test_agrupamento_invalido_ou_valor_diferente_nao_passam(idioma: str, texto: str) -> None:
    assert not numeros_confirmados(resposta(texto, idioma), dados({"affected_area_km2": 2500.25}))


@pytest.mark.parametrize("numero", [0, 1, 30, 51.35, 2026])
def test_fixture_sem_medicoes_nao_autoriza_numeros_de_coordenadas_flags_ou_ano(numero: float) -> None:
    colecao = json.loads(FIXTURE.read_text())
    hotspot = next(h["properties"] for h in colecao["features"] if h["properties"]["id"] == "guaiba-rs")
    resultado = {
        "hotspot": hotspot, "dados_de_exemplo": True,
        "metadata": {"sample": True}, "fonte": {"nome": "NISAR", "data": None},
    }

    texto = f"A área afetada é {str(numero).replace('.', ',')} km²."

    assert not numeros_confirmados(resposta(texto), dados(resultado))


def test_ausencia_de_medicao_e_preservada_sem_inventar_zero() -> None:
    valor = {"affected_area_km2": None, "confidence": None, "last_pass": None}

    assert numeros_confirmados(resposta(), dados(valor))
    assert not numeros_confirmados(resposta("O radar mediu 0 km²."), dados(valor))


def test_ids_urls_nomes_erros_e_proveniencia_nao_fornecem_medicao() -> None:
    valor = {
        "id": "212", "hotspot_id": "hotspot-212", "validos": ["212"],
        "url": "https://dados.test/212", "layers": {"flood": "layers/212/flood.png"},
        "name": {"pt": "Lago 212", "en": "Lake 212"},
        "fonte": {"nome": "Fonte 212", "data": "2026-11-12", "url": "https://fonte.test/212"},
    }

    assert not numeros_confirmados(resposta("O radar mediu 212 km²."), dados(valor))
    assert not numeros_confirmados(resposta("O radar mediu 2026 km²."), dados(valor))
    assert not numeros_confirmados(resposta("O radar mediu 12 km²."), dados(valor))
    assert not numeros_confirmados(resposta("O radar mediu 11 km²."), dados(valor))


@pytest.mark.parametrize("nome", ["212 km²", "NISAR 212 km²", "212", "212 pessoas"])
def test_nome_de_fonte_que_imita_quantidade_nao_esconde_numero_sem_evidencia(nome: str) -> None:
    valor = {"fonte": {"nome": nome, "data": None}}

    assert not numeros_confirmados(resposta(f"O radar mediu {nome}."), dados(valor))


def test_citacao_de_fonte_nao_exige_medicao_do_numero_no_nome() -> None:
    valor = {"fonte": {"nome": "Sentinel-1", "data": "2026-11-12"}}

    assert numeros_confirmados(resposta("O Sentinel-1 é uma das fontes de 12 nov 2026."), dados(valor))
    assert not numeros_confirmados(resposta("O Sentinel-1 mediu 1 km²."), dados(valor))


@pytest.mark.parametrize(
    "valor",
    [
        {"erro": "HTTP 503; timeout 5 s", "affected_area_km2": 18},
        {"disponivel": False, "affected_area_km2": 18},
        {"status": "error", "affected_area_km2": 18},
    ],
)
def test_falhas_e_dados_indisponiveis_nao_autorizam_medicao(valor: dict) -> None:
    assert not numeros_confirmados(resposta("Área de 18 km²."), dados(valor))


@pytest.mark.parametrize("texto", ["Confiança de **87%**.", "Confiança de 87 por cento."])
def test_confianca_decimal_autoriza_percentual_sem_transforma_lo_em_area(texto: str) -> None:
    valor = {"hotspot": {"confidence": 0.87}}

    assert numeros_confirmados(resposta(texto), dados(valor))
    assert not numeros_confirmados(resposta("Área de 87 km²."), dados(valor))
    assert not numeros_confirmados(resposta("Confiança de 88%."), dados(valor))


def test_percentual_usa_o_mesmo_arredondamento_do_alerta() -> None:
    assert numeros_confirmados(resposta("Confiança de 88%."), dados({"confidence": 0.875}))
    assert not numeros_confirmados(resposta("Confiança de 0,875%."), dados({"confidence": 0.875}))


@pytest.mark.parametrize(
    ("idioma", "texto"),
    [
        ("pt", "Passagem de 2026-11-12."),
        ("pt", "Passagem de 12/11/2026."),
        ("pt", "Passagem de 12 nov 2026."),
        ("pt", "Passagem de 12 de novembro de 2026."),
        ("en", "A pass from Nov 12, 2026."),
        ("en", "A pass from November 12, 2026."),
        ("en", "A pass from 11/12/2026."),
        ("pt", "Passagem em 12 nov."),
        ("en", "A pass on Nov 12."),
        ("pt", "O radar passou em 2026."),
    ],
)
def test_datas_so_autorizam_datas_da_ferramenta(idioma: str, texto: str) -> None:
    valor = {"hotspot": {"last_pass": "2026-11-12"}}

    assert numeros_confirmados(resposta(texto, idioma), dados(valor))


@pytest.mark.parametrize("texto", ["Passagem de 13 nov 2026.", "Passagem de 32 nov 2026.", "Passagem em 2025."])
def test_data_diferente_ou_invalida_nao_passa(texto: str) -> None:
    assert not numeros_confirmados(resposta(texto), dados({"last_pass": "2026-11-12"}))


def test_data_e_numero_sao_conferidos_separadamente() -> None:
    valor = {"affected_area_km2": 212.5, "last_pass": "2026-11-12"}

    assert numeros_confirmados(resposta(medido=["**212,5 km²**, passagem de 12 nov 2026."]), dados(valor))
    assert not numeros_confirmados(resposta(medido=["**12 km²**, passagem de 12 nov 2026."]), dados(valor))
    assert not numeros_confirmados(resposta("Em 2026 km² houve mudança."), dados(valor))


def test_instante_precisa_corresponder_ao_dado() -> None:
    valor = {"atualizada_em": "2026-11-15T09:00:00Z"}

    assert numeros_confirmados(resposta("Atualizada em 2026-11-15T09:00:00Z."), dados(valor))
    assert not numeros_confirmados(resposta("Atualizada em 2026-11-15T10:00:00Z."), dados(valor))


def test_previsao_em_texto_e_medicoes_aninhadas_sao_evidencia_sem_usar_o_proprio_modelo() -> None:
    resultados = [
        ("previsao_tempo", {"dias": [{"date": "2026-11-15", "precipitation_mm": 18}]}),
        ("exposicao", {"exposure": [{"kind": "people", "value": 2500}]}),
    ]

    assert numeros_confirmados(resposta("18 mm em 15 nov. Há 2.500 pessoas na área."), resultados)
    assert not numeros_confirmados(resposta("A resposta tem 99 pessoas e 99 aparece aqui."), resultados)
    assert not numeros_confirmados(resposta("O total é 2 dias."), resultados)


def test_rascunho_pode_conter_medicoes_e_datas_sem_virar_fonte_para_numeros_dos_links() -> None:
    valor = {
        "rascunho": "Alerta CassAAndra: Lago 999\nPrevisão: 18 mm em 15 nov.\n"
        "Fonte: Sentinel-1, 12 nov 2026.\nVeja no app: https://app.test/123",
        "fontes": [{"nome": "Sentinel-1", "data": "2026-11-12"}],
    }

    assert numeros_confirmados(resposta("Previsão de 18 mm em 15 nov."), dados(valor))
    for numero in [999, 1, 123]:
        assert not numeros_confirmados(resposta(f"Área de {numero} km²."), dados(valor))


@pytest.mark.parametrize("texto", ["A área é expressa em km².", "A área é expressa em m^2."])
def test_expoente_de_unidade_nao_e_um_numero_medido(texto: str) -> None:
    assert numeros_confirmados(resposta(texto), [])
    assert not numeros_confirmados(resposta("Área de 2 km²."), [])


@pytest.mark.parametrize(
    "texto",
    [
        "O El Niño causou a cheia.",
        "O El Niño provocou o fogo.",
        "O El Niño é responsável pela cheia.",
        "A cheia foi causada pelo El Niño.",
        "A cheia aconteceu por causa do El Niño.",
        "El Niño caused the flood.",
        "The flood was caused by El Niño.",
        "El Niño is responsible for the fire.",
        "The flood occurred due to El Niño.",
        "El Niño definitely caused the flood.",
        "Não há medição do radar, e o El Niño causou a cheia.",
        "There is no radar measurement, and El Niño caused the flood.",
        "Não sabemos a área, mas o El Niño causou a cheia.",
        "Não é hipótese: o El Niño causou a cheia.",
        "Não há dúvida de que o El Niño causou a cheia.",
        "Não existem dúvidas de que o El Niño causou o fogo.",
        "O radar não tem dados, por isso o El Niño causou a cheia.",
        "There is no radar measurement, therefore El Niño caused the flood.",
        "Não há medição, mas a cheia foi causada pelo El Niño.",
        "El Niño not only caused the flood, but also caused erosion.",
    ],
)
def test_afirmacao_causal_indevida_e_detectada(texto: str) -> None:
    assert afirma_causalidade(texto)


@pytest.mark.parametrize(
    "texto",
    [
        "O El Niño causou a cheia?",
        "Did El Niño cause the flood?",
        "O El Niño não causou a cheia.",
        "Não é possível afirmar que o El Niño causou a cheia.",
        "Não há evidência de que o El Niño causou o fogo.",
        "El Niño did not cause the flood.",
        "The flood was not caused by El Niño.",
        "There is no evidence that El Niño caused the flood.",
        "A hipótese é que o El Niño pode ter causado parte da mudança.",
        "Hipótese: o El Niño causou a cheia.",
        "El Niño may have caused the flood; this is a hypothesis.",
        "A flood caused by El Niño is only a hypothesis.",
        "A cheia causada pelo El Niño é apenas uma hipótese.",
        "Há uma hipótese regional.",
        "A chuva causou a cheia. O El Niño é um contexto regional.",
    ],
)
def test_perguntas_negacoes_e_hipoteses_nao_viram_afirmacao_causal(texto: str) -> None:
    assert not afirma_causalidade(texto)


def test_validacao_final_confere_medido_e_hipotese_alem_do_texto() -> None:
    fonte = dados({"affected_area_km2": 212})
    assert validar_resposta(resposta("Área de 212 km²."), fonte)
    assert not validar_resposta(resposta(medido=["Área de 999 km²."]), fonte)
    assert not validar_resposta(resposta(medido=["O El Niño causou a mudança."]), fonte)
    hipotese = Hipotese(texto="O El Niño causou a cheia.", evidencia="contexto regional", forca="fraca")
    assert not validar_resposta(resposta(hipotese=hipotese), fonte)


@pytest.mark.parametrize("idioma", ["pt", "en"])
def test_resposta_segura_retira_afirmacoes_e_preserva_fontes_sample_sem_mudar_original(idioma: str) -> None:
    original = resposta(
        "O El Niño causou uma mudança de 999 km².", idioma, ["Área de 999 km²."],
        Hipotese(texto="Causa certa", evidencia="sem dado", forca="forte"),
    )
    original.fontes = [Fonte(nome="NISAR", data="2026-11-12", ferramenta="consultar_hotspot")]
    original.dados_de_exemplo = True
    original.opcoes = ["Opção 999"]
    original.sugestoes = ["Pergunte sobre 999 km²"]
    antes = deepcopy(original)

    segura = resposta_segura(original)

    assert original == antes
    assert segura is not original
    assert segura.tipo == "resposta"
    assert segura.idioma == idioma
    assert segura.confianca == "baixa"
    assert segura.medido == [] and segura.hipotese is None
    assert segura.opcoes == [] and segura.sugestoes == []
    assert segura.fontes == original.fontes
    assert segura.fontes is not original.fontes
    assert segura.dados_de_exemplo is True
    assert validar_resposta(segura, [])
    assert ("Não consegui" in segura.texto) if idioma == "pt" else ("could not" in segura.texto)


# ---------- quais valores não foram confirmados, e onde ----------


def test_lista_o_valor_e_o_campo_de_cada_numero_ou_data_sem_apoio() -> None:
    r = resposta(
        "Desde 2024 a área cresceu.",
        medido=["**212 km²** em 12 nov 2026.", "Passagem de 13 nov 2026."],
        hipotese=Hipotese(texto="Chuva acima da média.", evidencia="Choveu 300 mm.", forca="fraca"),
    )
    r.opcoes = ["Ver 5 hotspots"]

    nao_confirmados = valores_nao_confirmados(r, dados({"affected_area_km2": 212, "last_pass": "2026-11-12"}))

    assert nao_confirmados == [
        NaoConfirmado(campo="texto", valor="2024"),
        NaoConfirmado(campo="medido[1]", valor="13 nov 2026"),
        NaoConfirmado(campo="opcoes[0]", valor="5"),
        NaoConfirmado(campo="hipotese.evidencia", valor="300"),
    ]


def test_lista_todos_os_valores_sem_repetir_e_o_percentual_com_o_sinal() -> None:
    r = resposta("Áreas de 10 km², 20 km² e de novo 10 km², com 88% de confiança.")

    nao_confirmados = valores_nao_confirmados(r, dados({"affected_area_km2": 212, "confidence": 0.86}))

    assert [n.valor for n in nao_confirmados] == ["10", "20", "88%"]
    assert valores_nao_confirmados(resposta("Área de 212 km²."), dados({"affected_area_km2": 212})) == []


def test_valor_fica_numa_linha_so_e_curto() -> None:
    longo = ".".join(["123"] * 40)
    r = resposta(f"Passagem de 13\nnov 2026 e {longo} km².")

    valores = [n.valor for n in valores_nao_confirmados(r, [])]

    assert valores[0] == "13 nov 2026"
    assert all("\n" not in v and len(v) <= 40 for v in valores)


def test_validacao_devolve_os_valores_e_os_campos_com_causa_afirmada() -> None:
    fonte = dados({"affected_area_km2": 212})

    recusada = validar_resposta(resposta("O El Niño causou a mudança de 999 km²."), fonte)
    aprovada = validar_resposta(resposta("Área de 212 km²."), fonte)

    assert not recusada
    assert recusada.nao_confirmados == (NaoConfirmado(campo="texto", valor="999"),)
    assert recusada.causais == ("texto",)
    assert aprovada
    assert aprovada.nao_confirmados == () and aprovada.causais == ()


def test_log_traz_o_valor_e_o_campo_sem_o_texto_inteiro(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="cass.verificacao")
    r = resposta("No Lago Guaíba, desde 2024 a área cresceu.", medido=["Área de **999 km²**."])
    hipotese = Hipotese(texto="O El Niño causou a cheia.", evidencia="contexto regional", forca="fraca")
    r.hipotese = hipotese

    validar_resposta(r, dados({"affected_area_km2": 212}))

    registro = " ".join(rec.getMessage() for rec in caplog.records)
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING
    assert "texto='2024'" in registro and "medido[0]='999'" in registro
    assert "hipotese.texto" in registro
    for trecho in ("Guaíba", "cresceu", "Área", "cheia", "regional"):
        assert trecho not in registro


def test_log_resume_quando_ha_muitos_valores(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="cass.verificacao")
    texto = " ".join(f"{n} km²." for n in range(101, 116))

    validar_resposta(resposta(texto), [])

    registro = caplog.records[0].getMessage()
    assert "'110'" in registro and "'111'" not in registro
    assert "e mais 5" in registro


def test_resposta_aprovada_nao_vai_para_o_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="cass.verificacao")

    validar_resposta(resposta("Área de 212 km²."), dados({"affected_area_km2": 212}))

    assert caplog.records == []


# ---------- baseline_pass e threshold_db do hotspot ----------


@pytest.mark.parametrize(
    ("idioma", "texto"),
    [
        ("pt", "Entre 14 jan 2026 e 12 nov 2026, o radar mediu **212 km²** com limiar de 3 dB."),
        ("en", "Between Jan 14, 2026 and Nov 12, 2026, the radar measured **212 km²** at a 3 dB threshold."),
    ],
)
def test_baseline_pass_e_threshold_db_confirmam_a_comparacao(idioma: str, texto: str) -> None:
    hotspot = {"affected_area_km2": 212, "last_pass": "2026-11-12", "baseline_pass": "2026-01-14", "threshold_db": 3}

    assert numeros_confirmados(resposta(texto, idioma), dados({"hotspot": hotspot}))
    assert not numeros_confirmados(resposta("Entre 15 jan 2026 e 12 nov 2026.", "pt"), dados({"hotspot": hotspot}))
