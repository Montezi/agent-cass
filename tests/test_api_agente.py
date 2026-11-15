"""Testes do POST /api/agente com um agente simulado (sem chamar o Gemini)."""

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from strands.hooks import BeforeToolCallEvent
from strands.interventions import Deny, Proceed

from cass import agente, ferramentas, main
from cass.config import Configuracao
from cass.hooks import ProgressoERegistro
from cass.guardrails import LimpaDados, PortaoDeFerramentas
from cass.modelos import RespostaAgente, RespostaCass, RespostaDoModelo
from cass.steering import SteeringCass

FIXTURE = Path(__file__).parent / "fixtures" / "hotspots.geojson"
URL_DADOS = "http://dados.teste/dados/hotspots.geojson"
HIPOTESE = {"texto": "chuva acima da média", "evidencia": "série de 2023", "forca": "fraca"}

# O modelo manda uma hipótese, mas nenhuma ferramenta devolveu dados de hipótese.
RESPOSTA_DO_MODELO = RespostaDoModelo(
    tipo="resposta",
    texto="No Guaíba, o radar ainda não tem medição.\n\nOs dados são de exemplo.",
    hipotese=HIPOTESE,
    confianca="baixa",
)


def troca_de_ferramenta(nome: str, resultado: dict, id_: str) -> list[dict]:
    """O par toolUse/toolResult que o Strands guarda em agent.messages."""
    return [
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": id_, "name": nome, "input": {}}}]},
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": id_,
                        "status": "success",
                        "content": [{"text": json.dumps(resultado)}],
                    }
                }
            ],
        },
    ]


class AgenteSimulado:
    def __init__(
        self,
        resposta: RespostaDoModelo | None = RESPOSTA_DO_MODELO,
        erro: Exception | None = None,
        demora_s: float = 0.0,
        trocas: list[dict] | None = None,
        respostas: list[RespostaDoModelo] | None = None,
    ) -> None:
        self.resposta, self.erro, self.demora_s = resposta, erro, demora_s
        self.trocas = trocas or []
        self.messages: list[dict] = []
        self.chamadas: list[dict[str, Any]] = []
        self.respostas = list(respostas) if respostas is not None else None

    async def invoke_async(self, prompt: str, **kwargs: Any) -> SimpleNamespace:
        self.chamadas.append({"prompt": prompt, **kwargs})
        self.messages.append({"role": "user", "content": [{"text": prompt}]})
        self.messages.extend(self.trocas)
        if self.demora_s:
            await asyncio.sleep(self.demora_s)
        if self.erro:
            raise self.erro
        resposta = self.respostas.pop(0) if self.respostas is not None else self.resposta
        parada = "end_turn" if resposta else "limit_structured_output_attempts"
        return SimpleNamespace(
            structured_output=resposta, stop_reason=parada,
            invocation_context=kwargs.get("invocation_context") or object(),
        )


@pytest.fixture
def criados() -> list[dict[str, Any]]:
    """Argumentos com que o endpoint montou cada agente."""
    return []


@pytest.fixture
def simular(
    monkeypatch: pytest.MonkeyPatch, criados: list[dict[str, Any]]
) -> Callable[..., AgenteSimulado]:
    def instalar(**opcoes: Any) -> AgenteSimulado:
        simulado = AgenteSimulado(**opcoes)

        def criar_agente_simulado(
            config: Configuracao,
            idioma: str = "pt",
            skills_injetadas: Any = (),
            progresso: ProgressoERegistro | None = None,
            portao: PortaoDeFerramentas | None = None,
            limpa_dados: LimpaDados | None = None,
            steering: SteeringCass | None = None,
        ) -> AgenteSimulado:
            criados.append(
                {
                    "config": config,
                    "idioma": idioma,
                    "skills_injetadas": list(skills_injetadas),
                    "progresso": progresso,
                    "portao": portao,
                    "limpa_dados": limpa_dados,
                    "steering": steering,
                }
            )
            return simulado

        monkeypatch.setattr(agente, "criar_agente", criar_agente_simulado)
        return simulado

    return instalar


@pytest.fixture
def dados(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(URL_DADOS).mock(
        return_value=httpx.Response(200, content=FIXTURE.read_bytes())
    )


@pytest.fixture
def cliente(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    monkeypatch.setenv("DADOS_BASE", "http://dados.teste/dados")
    ferramentas.limpar_cache()
    with TestClient(main.app) as c:
        yield c
    ferramentas.limpar_cache()


def perguntar(
    cliente: TestClient,
    question: str = "O que mudou no Guaíba?",
    hotspot_id: str | None = "guaiba-rs",
    language: str = "pt",
    status: int = 200,
) -> RespostaAgente:
    corpo: dict[str, Any] = {"question": question, "language": language}
    if hotspot_id is not None:
        corpo["hotspot_id"] = hotspot_id
    resposta = cliente.post("/api/agente", json=corpo)
    assert resposta.status_code == status, resposta.text
    return RespostaAgente.model_validate(resposta.json())


# ---------- resposta normal ----------


def test_json_valida_contra_o_contrato(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simular()

    resposta = perguntar(cliente)

    # Sem o parágrafo de hipótese: nenhuma ferramenta devolveu dados de hipótese.
    assert resposta.paragraphs == [
        "No Guaíba, o radar ainda não tem medição.",
        "Os dados são de exemplo.",
    ]
    assert resposta.sources == ["NISAR, data não informada"]


def test_agente_recebe_o_esquema_e_o_limite_de_tentativas(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simulado = simular()

    perguntar(cliente)

    chamada = simulado.chamadas[0]
    assert chamada["structured_output_model"] is RespostaDoModelo
    assert chamada["limits"] == {
        "turns": 8, "output_tokens": 8192, "total_tokens": 60000,
        "structured_output_attempts": 3,
    }


def test_modelo_nao_preenche_fontes_idioma_nem_dados_de_exemplo() -> None:
    do_codigo = {"idioma", "fontes", "dados_de_exemplo"}

    assert set(RespostaDoModelo.model_fields) == set(RespostaCass.model_fields) - do_codigo
    for nome, campo in RespostaDoModelo.model_fields.items():
        assert campo.annotation == RespostaCass.model_fields[nome].annotation


def test_usa_a_config_da_subida_e_o_idioma_do_pedido(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    criados: list[dict],
) -> None:
    simular()

    perguntar(cliente, "What changed at Lake Kariba?", "kariba", "en")

    assert criados[0]["config"].google_cloud_project == "projeto-teste"
    assert criados[0]["idioma"] == "en"


# ---------- hotspot buscado antes, na mensagem ----------


def test_dados_do_hotspot_vao_na_mensagem(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simulado = simular()

    perguntar(cliente, "O que mudou?", "guaiba-rs")

    mensagem = simulado.chamadas[0]["prompt"]
    assert mensagem.endswith("Pergunta: O que mudou?")
    bloco = mensagem.split("\n")[1]
    dados_na_mensagem = json.loads(bloco)
    assert dados_na_mensagem["hotspot"]["id"] == "guaiba-rs"
    assert dados_na_mensagem["hotspot"]["affected_area_km2"] is None
    assert dados_na_mensagem["dados_de_exemplo"] is True


def test_hotspot_antecipado_e_limpo_antes_de_entrar_na_mensagem(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    monkeypatch: pytest.MonkeyPatch,
    ids_conhecidos: list[str],
) -> None:
    original = {
        "hotspot": {
            "id": "guaiba-rs", "name": {"pt": "á" * 250, "en": "b" * 250},
            "affected_area_km2": 212, "last_pass": "2026-11-12",
        },
        "fonte": {"nome": "NISAR", "data": "2026-11-12", "url": "https://dados.teste/" + "a" * 250},
        "dados_de_exemplo": True,
    }
    antes = deepcopy(original)

    async def buscar(hotspot_id: str) -> dict:
        return original

    monkeypatch.setattr(agente, "_buscar_hotspot", buscar)
    simulado = simular()

    resposta = perguntar(cliente, question="O que mudou?")

    dados = json.loads(simulado.chamadas[0]["prompt"].split("\n")[1])
    assert dados["_tipo"] == "dado"
    assert dados["hotspot"]["name"] == {"pt": "á" * 200, "en": "b" * 200}
    assert dados["hotspot"]["affected_area_km2"] == 212
    assert dados["fonte"] == original["fonte"]
    assert original == antes
    assert resposta.sources == ["NISAR, 12 nov 2026"]
    assert resposta.note is not None


def test_hotspot_antecipado_compartilha_a_cota_das_seis_consultas(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    criados: list[dict],
) -> None:
    simular()
    perguntar(cliente)
    portao = criados[0]["portao"]
    assert portao.chamadas == 1

    for numero in range(2, 8):
        evento = BeforeToolCallEvent(
            agent=SimpleNamespace(), selected_tool=None,
            tool_use={"toolUseId": f"consulta-{numero}", "name": "consultar_hotspot", "input": {}},
            invocation_state={},
        )
        decisao = portao.before_tool_call(evento)
        assert isinstance(decisao, Proceed if numero <= 6 else Deny)


@pytest.mark.parametrize("hotspot_id", [None, "guaiba-rs"])
def test_cada_pergunta_ganha_guardrails_novos(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    criados: list[dict],
    hotspot_id: str | None,
) -> None:
    simular()

    perguntar(cliente, hotspot_id=hotspot_id)
    perguntar(cliente, hotspot_id=hotspot_id)

    assert criados[0]["portao"] is not criados[1]["portao"]
    assert criados[0]["limpa_dados"] is not criados[1]["limpa_dados"]
    assert all(c["portao"].chamadas == int(hotspot_id is not None) for c in criados)


def test_hotspot_buscado_conta_como_ferramenta_usada(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simular()  # o modelo simulado não chama nenhuma ferramenta

    assert perguntar(cliente).tools == ["consultar_hotspot"]


def test_consulta_antecipada_publica_progresso_e_registra_tempo(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    criados: list[dict],
    capsys: pytest.CaptureFixture[str],
) -> None:
    simular()

    perguntar(cliente)

    progresso = criados[0]["progresso"]
    assert isinstance(progresso, ProgressoERegistro)
    assert progresso.fila.get_nowait() == ("pensando", {})
    assert progresso.fila.get_nowait() == ("ferramenta", {"nome": "consultar_hotspot"})
    assert progresso.fila.get_nowait() == ("pensando", {})
    assert progresso.fila.empty()
    linhas = capsys.readouterr().out.splitlines()
    assert len(linhas) == 1
    registro = json.loads(linhas[0])
    assert registro["tipo"] == "resposta"
    assert [f["nome"] for f in registro["ferramentas"]] == ["consultar_hotspot"]
    assert 0 <= registro["ferramentas"][0]["tempo_s"] <= registro["tempo_total_s"]


def test_stream_usa_o_mesmo_responder_e_o_mesmo_contrato(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    criados: list[dict],
    capsys: pytest.CaptureFixture[str],
) -> None:
    simular()
    pedido = {"question": "O que mudou?", "hotspot_id": "guaiba-rs", "language": "pt"}
    esperado = cliente.post("/api/agente", json=pedido).json()

    http = cliente.post("/api/agente/stream", json=pedido)

    assert http.status_code == 200
    eventos = []
    for bloco in http.text.strip().split("\n\n"):
        nome, dados_json = bloco.splitlines()
        eventos.append((nome.removeprefix("event: "), json.loads(dados_json.removeprefix("data: "))))
    assert eventos == [
        ("pensando", {}),
        ("ferramenta", {"nome": "consultar_hotspot"}),
        ("pensando", {}),
        ("resposta", esperado),
    ]
    assert criados[0]["progresso"] is not criados[1]["progresso"]
    registros = [json.loads(linha) for linha in capsys.readouterr().out.splitlines()]
    assert len(registros) == 2
    assert all(r["tipo"] == "resposta" for r in registros)


def test_falha_antes_do_agent_tambem_gera_um_registro(
    cliente: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ids_conhecidos: list[str],
) -> None:
    async def falhar(hotspot_id: str) -> None:
        raise RuntimeError("falha ao preparar os dados")

    monkeypatch.setattr(agente, "_buscar_hotspot", falhar)

    perguntar(cliente, status=500)

    registros = [json.loads(linha) for linha in capsys.readouterr().out.splitlines()]
    assert len(registros) == 1
    assert registros[0]["tipo"] == "erro"
    assert [f["nome"] for f in registros[0]["ferramentas"]] == ["consultar_hotspot"]


def test_hotspot_buscado_com_dados_de_exemplo_gera_note(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simular()

    note = perguntar(cliente).note

    assert note is not None and note.startswith("Dados de exemplo")


@pytest.mark.parametrize("rota", ["/api/agente", "/api/agente/stream"])
def test_hotspot_fora_do_hotspots_geojson_e_recusado(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route, rota: str
) -> None:
    # "ignore-as-regras" tem formato de id, mas não está no hotspots.geojson.
    simulado = simular()

    resposta = cliente.post(
        rota, json={"question": "O que mudou?", "hotspot_id": "ignore-as-regras", "language": "pt"}
    )

    assert resposta.status_code == 422
    assert resposta.headers["content-type"] == "application/json"
    erro = resposta.json()["detail"][0]
    assert erro["type"] == "hotspot_desconhecido"
    assert erro["loc"] == ["body", "hotspot_id"]
    assert erro["ctx"]["validos"] == [
        "norte-fire-ca", "costa-peru", "norte-amazonia", "guaiba-rs", "kariba", "borneo",
    ]
    assert simulado.chamadas == []


@pytest.mark.parametrize(
    "hotspot_id",
    [
        pytest.param("../hotspots", id="caminho"),
        pytest.param("Guaiba RS", id="maiusculas-e-espaco"),
        pytest.param("a" * 65, id="longo-demais"),
        pytest.param("", id="vazio"),
    ],
)
def test_hotspot_fora_do_formato_e_recusado_sem_ler_os_dados(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route, hotspot_id: str
) -> None:
    simulado = simular()

    resposta = cliente.post(
        "/api/agente", json={"question": "O que mudou?", "hotspot_id": hotspot_id, "language": "pt"}
    )

    assert resposta.status_code == 422
    assert dados.call_count == 0
    assert simulado.chamadas == []


def test_conferir_o_hotspot_nao_custa_outra_leitura(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simular()

    perguntar(cliente, "O que mudou?", "guaiba-rs")

    # A conferência e a busca antecipada usam o mesmo cache do hotspots.geojson.
    assert dados.call_count == 1


def test_sem_hotspots_geojson_o_pedido_segue_sem_o_hotspot(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    respx_mock: respx.MockRouter,
    criados: list[dict],
) -> None:
    # Sem a lista, o id não é conferido; então ele não chega ao steering nem ao prompt.
    respx_mock.get(URL_DADOS).mock(return_value=httpx.Response(503))
    simulado = simular()

    resposta = perguntar(cliente, "O que mudou?", "guaiba-rs")

    assert simulado.chamadas[0]["prompt"] == "O que mudou?"
    assert resposta.tools == []
    assert criados[0]["steering"].hotspot_aberto is None


def test_sem_hotspot_nao_busca_os_dados(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simulado = simular()

    perguntar(cliente, "Quais hotspots existem?", hotspot_id=None)

    assert dados.call_count == 0
    assert simulado.chamadas[0]["prompt"] == "Quais hotspots existem?"


def test_dados_fora_do_ar_nao_impedem_a_pergunta(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(URL_DADOS).mock(return_value=httpx.Response(503))
    simulado = simular()

    perguntar(cliente, "O que mudou?", "guaiba-rs")

    assert simulado.chamadas[0]["prompt"] == "O que mudou?"


# ---------- skills injetadas por palavra-chave ----------


def test_pergunta_de_alerta_leva_a_skill_na_mensagem(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    criados: list[dict],
) -> None:
    simulado = simular()

    perguntar(cliente, "Monte o alerta para o Guaíba")

    mensagem = simulado.chamadas[0]["prompt"]
    assert mensagem.startswith("Instruções da skill alerta-whatsapp (já ativada;")
    assert "Chame `rascunhar_alerta`" in mensagem
    assert "Dados do hotspot aberto no app" in mensagem
    assert mensagem.endswith("Pergunta: Monte o alerta para o Guaíba")
    assert criados[0]["skills_injetadas"] == ["alerta-whatsapp"]


def test_pergunta_sem_palavra_chave_nao_leva_skill(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    criados: list[dict],
) -> None:
    simulado = simular()

    perguntar(cliente, "O que mudou?")

    assert "Instruções da skill" not in simulado.chamadas[0]["prompt"]
    assert criados[0]["skills_injetadas"] == []


def test_duas_skills_na_mesma_pergunta(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], criados: list[dict]
) -> None:
    simulado = simular()

    perguntar(cliente, "O El Niño causou isso? Como o radar viu?", hotspot_id=None)

    mensagem = simulado.chamadas[0]["prompt"]
    assert mensagem.index("skill contexto-el-nino") < mensagem.index("skill explicar-radar")
    assert criados[0]["skills_injetadas"] == ["contexto-el-nino", "explicar-radar"]


# ---------- tools e note vêm do que aconteceu, não do modelo ----------


def test_ferramentas_do_modelo_entram_em_tools_sem_repetir(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simular(
        trocas=[
            *troca_de_ferramenta("listar_hotspots", {"dados_de_exemplo": True}, "t1"),
            *troca_de_ferramenta("consultar_hotspot", {"dados_de_exemplo": True}, "t2"),
            *troca_de_ferramenta("RespostaCass", {}, "t3"),
        ]
    )

    assert perguntar(cliente).tools == ["consultar_hotspot", "listar_hotspots"]


def test_sem_ferramentas_nao_ha_note_nem_fontes(
    cliente: TestClient, simular: Callable[..., AgenteSimulado]
) -> None:
    simular()

    resposta = perguntar(cliente, hotspot_id=None)

    assert (resposta.note, resposta.sources, resposta.tools) == (None, [], [])


def test_dados_de_exemplo_vindos_de_ferramenta_do_modelo(
    cliente: TestClient, simular: Callable[..., AgenteSimulado]
) -> None:
    simular(trocas=troca_de_ferramenta("listar_hotspots", {"dados_de_exemplo": True}, "t1"))

    resposta = perguntar(cliente, "Which hotspots?", hotspot_id=None, language="en")

    assert resposta.note is not None and resposta.note.startswith("Sample data")


def test_resultado_de_ferramenta_que_nao_e_de_dados_nao_conta(
    cliente: TestClient, simular: Callable[..., AgenteSimulado]
) -> None:
    simular(trocas=troca_de_ferramenta("RespostaCass", {"dados_de_exemplo": True}, "t1"))

    assert perguntar(cliente, hotspot_id=None).note is None


# ---------- hipótese só com dados de hipótese de uma ferramenta ----------


def test_hipotese_indisponivel_nao_deixa_a_do_modelo_passar(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    sem_stats = {"disponivel": False, "motivo": "ainda não calculado pelo pipeline"}
    simular(trocas=troca_de_ferramenta("hipotese", sem_stats, "t1"))

    resposta = perguntar(cliente)

    assert not any(p.startswith("Hipótese") for p in resposta.paragraphs)


def test_contexto_do_el_nino_nao_e_dado_de_hipotese(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    contexto = {"enso_effect": {"pt": "chuva acima da média", "en": "above-average rainfall"}}
    simular(trocas=troca_de_ferramenta("contexto_enso", contexto, "t1"))

    assert not any(p.startswith("Hipótese") for p in perguntar(cliente).paragraphs)


def test_hipotese_fica_quando_a_ferramenta_devolveu_dados(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    com_dados = {"disponivel": True, "hipotese": {"text": "chuva", "evidence": "série", "strength": "weak", "date": "2023-01-01"}}
    simular(trocas=troca_de_ferramenta("hipotese", com_dados, "t1"))

    resposta = perguntar(cliente)

    assert resposta.paragraphs[-1] == (
        "Hipótese (força fraca): chuva acima da média. Evidência: série de 2023."
    )


# ---------- fontes montadas pelo código ----------


def test_fontes_vem_das_ferramentas_com_a_data_de_cada_uma(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    previsao = {"fonte": {"nome": "Meteomatics", "data": "2026-11-14", "url": "http://x"}}
    simular(trocas=troca_de_ferramenta("previsao_tempo", previsao, "t1"))

    assert perguntar(cliente).sources == [
        "NISAR, data não informada",
        "Meteomatics, 14 nov 2026",
    ]


def test_fonte_do_hotspot_usa_o_last_pass(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], respx_mock: respx.MockRouter
) -> None:
    dados = json.loads(FIXTURE.read_text(encoding="utf-8"))
    dados["features"][3]["properties"]["last_pass"] = "2026-11-12"
    respx_mock.get(URL_DADOS).mock(return_value=httpx.Response(200, json=dados))
    simular()

    assert perguntar(cliente).sources == ["NISAR, 12 nov 2026"]


def test_fontes_em_ingles(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simular()

    assert perguntar(cliente, language="en").sources == ["NISAR, date not provided"]


def test_resultado_com_erro_nao_vira_fonte(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    falhou = {"erro": "a /api/clima não respondeu em 5 s", "url": "http://x"}
    simular(trocas=troca_de_ferramenta("previsao_tempo", falhou, "t1"))

    resposta = perguntar(cliente)

    assert resposta.sources == ["NISAR, data não informada"]
    assert resposta.tools == ["consultar_hotspot", "previsao_tempo"]


def test_fontes_do_rascunho_de_alerta(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    alerta = {
        "rascunho": "…",
        "fontes": [
            {"nome": "NISAR", "data": None, "url": "http://x"},
            {"nome": "Open-Meteo", "data": "2026-11-13", "url": "http://y"},
        ],
    }
    simular(trocas=troca_de_ferramenta("rascunhar_alerta", alerta, "t1"))

    assert perguntar(cliente).sources == ["NISAR, data não informada", "Open-Meteo, 13 nov 2026"]


# ---------- falhas: 500 e 504 com corpo no formato do contrato ----------


def test_agente_que_falha_devolve_500(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simular(erro=RuntimeError("Vertex AI fora do ar"))

    resposta = perguntar(cliente, status=500)

    assert "Tente de novo" in resposta.paragraphs[0]
    assert "Vertex" not in resposta.paragraphs[0]
    assert (resposta.tools, resposta.sources) == ([], [])


def test_sem_saida_estruturada_devolve_500(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simular(resposta=None)

    perguntar(cliente, status=500)


def test_resposta_sem_texto_devolve_500(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], dados: respx.Route
) -> None:
    simular(resposta=RESPOSTA_DO_MODELO.model_copy(update={"texto": " "}))

    perguntar(cliente, status=500)


def test_agente_lento_sem_hotspot_consultado_devolve_504(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agente, "TEMPO_LIMITE_S", 0.05)
    simular(demora_s=5)

    inicio = time.monotonic()
    resposta = perguntar(cliente, "What changed?", None, "en", status=504)

    assert time.monotonic() - inicio < 2
    assert "try again" in resposta.paragraphs[0]


def test_agente_lento_com_hotspot_consultado_devolve_a_resposta_segura(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agente, "TEMPO_LIMITE_S", 0.05)
    simular(demora_s=5)

    inicio = time.monotonic()
    resposta = perguntar(cliente, "What changed?", "kariba", "en", status=200)

    assert time.monotonic() - inicio < 2
    # O fixture ainda não tem medição: a resposta segura diz isso, com a fonte.
    assert resposta.paragraphs[0].startswith("The radar has no measurement for ")
    assert resposta.tools == ["consultar_hotspot"]
    assert resposta.sources and resposta.note


def test_tempo_limite_e_de_35_segundos() -> None:
    # O app espera 45 s (frontend, PR #15) e o Cloud Run corta em 60 s: o 504 do
    # Cass precisa chegar antes dos dois.
    assert agente.TEMPO_LIMITE_S == 35
    assert agente.TEMPO_LIMITE_S < 45 < 60


def test_json_e_stream_usam_o_mesmo_prazo() -> None:
    assert main.TEMPO_LIMITE_S is agente.TEMPO_LIMITE_S


# ---------- camada 4: mesma execução, uma correção e resposta segura ----------


def test_numero_sem_dado_pede_uma_correcao_com_o_mesmo_agente(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    criados: list[dict],
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventada = RESPOSTA_DO_MODELO.model_copy(update={"texto": "O radar mediu **999 km²**."})
    simulado = simular(respostas=[inventada, RESPOSTA_DO_MODELO])

    resposta = perguntar(cliente)

    assert "999" not in " ".join(resposta.paragraphs)
    assert len(simulado.chamadas) == 2 and len(criados) == 1
    assert simulado.chamadas[1]["prompt"].startswith("Revise a resposta")
    assert criados[0]["portao"].chamadas == 1  # consulta antecipada feita uma vez
    registros = [json.loads(linha) for linha in capsys.readouterr().out.splitlines()]
    assert len(registros) == 1 and registros[0]["tipo"] == "resposta"


def test_correcao_lista_cada_valor_sem_apoio(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
) -> None:
    inventada = RESPOSTA_DO_MODELO.model_copy(update={
        "texto": "Desde 2024 o radar mediu **999 km²**.", "medido": ["Mais **999 km²**."],
    })
    simulado = simular(respostas=[inventada, RESPOSTA_DO_MODELO])

    perguntar(cliente)

    correcao = simulado.chamadas[1]["prompt"]
    assert correcao.startswith("Revise a resposta")
    for valor in ("2024", "999"):
        assert f"o valor {valor} não está nos resultados; remova ou use um valor consultado" in correcao
    assert correcao.count("o valor 999") == 1
    assert "El Niño" not in correcao
    assert "Desde" not in correcao and "radar mediu" not in correcao


def test_correcao_de_causa_afirmada_pede_hipotese_sem_listar_valores(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
) -> None:
    causal = RESPOSTA_DO_MODELO.model_copy(update={"texto": "O El Niño causou a mudança."})
    simulado = simular(respostas=[causal, RESPOSTA_DO_MODELO])

    perguntar(cliente)

    correcao = simulado.chamadas[1]["prompt"]
    assert correcao.startswith("Revise a resposta")
    assert "hipótese" in correcao and "o valor" not in correcao


def test_correcao_lista_no_maximo_oito_valores(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
) -> None:
    texto = " ".join(f"{n} km²." for n in range(101, 113))
    simulado = simular(respostas=[
        RESPOSTA_DO_MODELO.model_copy(update={"texto": texto}), RESPOSTA_DO_MODELO,
    ])

    perguntar(cliente)

    correcao = simulado.chamadas[1]["prompt"]
    assert correcao.count("não está nos resultados") == agente.MAX_VALORES_NA_CORRECAO == 8
    assert "o valor 108 " in correcao and "o valor 109 " not in correcao


@pytest.mark.parametrize("idioma", ["pt", "en"])
def test_segunda_saida_sem_apoio_usa_template_de_baixa_confianca(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
    idioma: str,
) -> None:
    inventada = RESPOSTA_DO_MODELO.model_copy(update={
        "texto": "O radar mediu **999 km²**.", "medido": ["Área de **999 km²**"],
        "sugestoes": ["Explique os 999 km²"],
    })
    simulado = simular(respostas=[inventada, inventada])
    internas = []
    original = main.responder

    async def capturar(*args: Any, **kwargs: Any) -> tuple:
        resultado = await original(*args, **kwargs)
        internas.append(resultado[0])
        return resultado

    monkeypatch.setattr(main, "responder", capturar)

    resposta = perguntar(cliente, language=idioma)

    assert len(simulado.chamadas) == 2
    assert internas[0].confianca == "baixa"
    assert internas[0].medido == [] and internas[0].hipotese is None
    assert internas[0].sugestoes == []
    assert "999" not in " ".join(resposta.paragraphs)
    # O fixture ainda não tem medição: a resposta montada pelo código diz isso.
    assert resposta.paragraphs[0] == {
        "pt": "O radar ainda não tem medição para Lago Guaíba, RS.",
        "en": "The radar has no measurement for Guaíba Lake, RS (Brazil) yet.",
    }[idioma]
    assert resposta.tools == ["consultar_hotspot"]
    assert resposta.sources and resposta.note
    assert ("Não consegui confirmar" if idioma == "pt" else "I could not confirm") in resposta.paragraphs[-1]


def test_prazo_nao_reinicia_na_correcao_da_camada_quatro(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agente, "TEMPO_LIMITE_S", 0.2)
    inventada = RESPOSTA_DO_MODELO.model_copy(update={"texto": "Área de 999 km²."})
    simulado = simular(respostas=[inventada, RESPOSTA_DO_MODELO], demora_s=0.12)

    # Sem prefetch de rede: duas chamadas de 0,12 s caberiam em dois prazos
    # separados, mas ultrapassam o único prazo de 0,2 s compartilhado.
    perguntar(cliente, hotspot_id=None, status=504)

    assert len(simulado.chamadas) == 2


def test_stream_tambem_entrega_apenas_a_resposta_verificada(
    cliente: TestClient,
    simular: Callable[..., AgenteSimulado],
    dados: respx.Route,
) -> None:
    inventada = RESPOSTA_DO_MODELO.model_copy(update={"texto": "Área de 999 km²."})
    simulado = simular(respostas=[inventada, inventada])

    http = cliente.post("/api/agente/stream", json={
        "question": "O que mudou?", "language": "pt", "hotspot_id": "guaiba-rs",
    })

    assert http.status_code == 200
    assert "999" not in http.text
    assert http.text.count("event: resposta") == 1
    assert "Não consegui confirmar" in http.text
    assert len(simulado.chamadas) == 2


# ---------- pedido inválido e rota antiga ----------


@pytest.mark.parametrize(
    "corpo",
    [
        pytest.param({"mensagens": [{"papel": "user", "texto": "oi"}]}, id="formato-antigo"),
        pytest.param({"hotspot_id": "guaiba-rs", "language": "pt"}, id="sem-question"),
        pytest.param({"question": "oi", "language": "es"}, id="language-es"),
        pytest.param({"question": "x" * 501, "language": "pt"}, id="mais-de-500"),
    ],
)
def test_pedido_invalido_e_recusado(
    cliente: TestClient, simular: Callable[..., AgenteSimulado], corpo: dict
) -> None:
    simulado = simular()

    assert cliente.post("/api/agente", json=corpo).status_code == 422
    assert simulado.chamadas == []


def test_rota_chat_nao_existe_mais(cliente: TestClient) -> None:
    resposta = cliente.post("/chat", json={"question": "oi", "language": "pt"})

    assert resposta.status_code == 404
