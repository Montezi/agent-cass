"""Camadas de proteção com Agent real, mantendo a mesma cota entre correções."""

import asyncio
import json
import time
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from strands import tool
from strands.types.exceptions import MaxTokensReachedException

from cass import agente as modulo_agente
from cass import main
from cass.config import carregar_config
from cass.contrato import para_contrato
from cass.hooks import ProgressoERegistro
from cass.modelos import PedidoAgente, RespostaAgente
from cass.verificacao import afirma_causalidade, validar_resposta
from test_api_agente_stream import _eventos
from test_guardrails_integracao import ModeloProgramado, chamada

DADOS = {
    "fonte": {"nome": "NISAR", "data": "2026-11-12", "url": "https://dados.teste"},
    "dados_de_exemplo": True,
    "hotspot": {"id": "guaiba-rs", "affected_area_km2": 212, "last_pass": "2026-11-12"},
}
PEDIDO = PedidoAgente(question="O que mudou no Guaíba?", hotspot_id="guaiba-rs", language="pt")


def saida(identificador: str, texto: str, **campos: Any) -> dict:
    return chamada(
        "RespostaDoModelo", identificador, tipo="resposta", texto=texto,
        confianca="alta", **campos,
    )


def configurar(
    monkeypatch: pytest.MonkeyPatch, modelo: ModeloProgramado, ferramentas: list,
) -> tuple[list[dict], AsyncMock]:
    monkeypatch.setenv("DECIDER_ATIVO", "false")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "teste-local")
    monkeypatch.setattr(modulo_agente, "criar_modelo", lambda config: modelo)
    monkeypatch.setattr(modulo_agente, "carregar_skills", lambda: ())
    monkeypatch.setattr(modulo_agente, "FERRAMENTAS_ATIVAS", ferramentas)
    prefetch = AsyncMock(return_value=deepcopy(DADOS))
    monkeypatch.setattr(modulo_agente, "_buscar_hotspot", prefetch)
    criar_original = modulo_agente.criar_agente
    criados = []

    def criar(*args: Any, **kwargs: Any):
        agente = criar_original(*args, **kwargs)
        criados.append({"agente": agente, **kwargs})
        return agente

    monkeypatch.setattr(modulo_agente, "criar_agente", criar)
    return criados, prefetch


def test_correcao_numerica_compartilha_agente_prefetch_cota_e_registro(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    executadas = []

    @tool
    async def consultar_hotspot(id: str) -> dict:
        """Retorna uma medição local para exercitar a cota real do Agent."""
        executadas.append(id)
        return deepcopy(DADOS)

    @tool
    async def previsao_tempo(id: str) -> dict:
        """Esta consulta nova deve ser bloqueada na segunda invocação."""
        pytest.fail("A correção não pode ganhar uma nova cota de consultas.")

    modelo = ModeloProgramado([
        [chamada("consultar_hotspot", f"consulta-{i}", id="guaiba-rs") for i in range(5)],
        [saida("primeira-saida", "O radar mediu **999 km²**.")],
        [chamada("previsao_tempo", "consulta-sem-cota", id="guaiba-rs")],
        [saida("saida-corrigida", "O radar mediu a área do hotspot.",
               medido=["**212 km²** em 12 nov 2026."])],
    ])
    criados, prefetch = configurar(monkeypatch, modelo, [consultar_hotspot, previsao_tempo])
    progresso = ProgressoERegistro()

    resposta, ferramentas = asyncio.run(modulo_agente.responder(
        PEDIDO, carregar_config(), progresso=progresso, validar=validar_resposta,
    ))

    prefetch.assert_awaited_once_with("guaiba-rs")
    assert len(criados) == 1
    assert criados[0]["progresso"] is progresso
    assert executadas == ["guaiba-rs"] * 5  # Junto do prefetch, seis consultas reais.
    assert criados[0]["portao"].chamadas == 7
    assert criados[0]["portao"].negadas == {"consulta-sem-cota"}
    assert ferramentas == ["consultar_hotspot"]
    assert modelo.roteiro == [] and len(modelo.recebidas) == 4
    assert any("Revise a resposta" in str(m) for m in modelo.recebidas[2])
    assert resposta.texto == "O radar mediu a área do hotspot."
    assert resposta.medido == ["**212 km²** em 12 nov 2026."]
    assert resposta.confianca == "alta"
    assert resposta.dados_de_exemplo is True
    assert len(resposta.fontes) == 6
    assert all(f.nome == "NISAR" and f.data == "2026-11-12" for f in resposta.fontes)
    assert all(f.ferramenta == "consultar_hotspot" for f in resposta.fontes)
    resposta_publica = para_contrato(resposta, ferramentas)
    assert resposta_publica.tools == ["consultar_hotspot"]
    assert resposta_publica.sources == ["NISAR, 12 nov 2026"]
    assert resposta_publica.note and resposta_publica.note.startswith("Dados de exemplo")
    assert "999" not in resposta_publica.model_dump_json()
    linhas = capsys.readouterr().out.splitlines()
    assert len(linhas) == 1
    registro = json.loads(linhas[0])
    assert registro["tipo"] == "resposta"
    assert len(registro["ferramentas"]) == 7  # O hook observa também a tentativa negada.
    assert registro["tempo_total_s"] >= 0


def test_causalidade_persistente_para_apos_guide_e_uma_correcao_da_camada_final(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    texto = "O El Niño causou a mudança."
    modelo = ModeloProgramado([
        [saida("descartada-pelo-steering", texto)],
        [saida("primeira-resposta-causal", texto)],
        [saida("segunda-resposta-causal", texto)],
    ])
    criados, prefetch = configurar(monkeypatch, modelo, [])

    resposta, ferramentas = asyncio.run(modulo_agente.responder(
        PEDIDO, carregar_config(), validar=validar_resposta,
    ))

    assert len(criados) == 1
    prefetch.assert_awaited_once_with("guaiba-rs")
    assert modelo.roteiro == [] and len(modelo.recebidas) == 3
    assert criados[0]["steering"].correcoes_causalidade == 1
    assert criados[0]["portao"].chamadas == 1
    assert not any("descartada-pelo-steering" in str(m) for m in criados[0]["agente"].messages)
    assert any("Revise a resposta" in str(m) for m in modelo.recebidas[2])
    assert resposta.confianca == "baixa"
    assert resposta.medido == [] and resposta.hipotese is None
    assert not afirma_causalidade(resposta.texto)
    assert resposta.texto != texto
    assert resposta.dados_de_exemplo is True
    assert ferramentas == ["consultar_hotspot"]
    assert [(f.nome, f.data) for f in resposta.fontes] == [("NISAR", "2026-11-12")]
    assert validar_resposta(resposta, [("consultar_hotspot", DADOS)])
    linhas = capsys.readouterr().out.splitlines()
    assert len(linhas) == 1 and json.loads(linhas[0])["tipo"] == "resposta"


def test_segunda_falha_responde_com_os_dados_consultados(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    medido = deepcopy(DADOS)
    medido["hotspot"].update(
        name={"pt": "Lago Guaíba, RS", "en": "Guaíba Lake, RS (Brazil)"},
        dominant_class="flood", confidence=0.86, baseline_pass="2026-01-14",
    )
    modelo = ModeloProgramado([
        [saida("primeira-inventada", "Desde 2024 o radar mediu **999 km²**.")],
        [saida("segunda-inventada", "O radar mediu **998 km²**.")],
    ])
    _, prefetch = configurar(monkeypatch, modelo, [])
    prefetch.return_value = medido

    resposta, ferramentas = asyncio.run(modulo_agente.responder(
        PEDIDO, carregar_config(), validar=validar_resposta,
    ))

    publica = para_contrato(resposta, ferramentas)
    assert publica.paragraphs[0] == (
        "Entre 14 jan 2026 e 12 nov 2026, o radar mediu **212 km²** de cheia em Lago Guaíba, RS. "
        "Confiança do modelo: **86%** (alta)."
    )
    assert publica.sources == ["NISAR, 12 nov 2026"]
    assert publica.note and publica.note.startswith("Dados de exemplo")
    assert "999" not in publica.model_dump_json() and "998" not in publica.model_dump_json()
    assert validar_resposta(resposta, [("consultar_hotspot", medido)])


def medido(**campos: Any) -> dict:
    dados = deepcopy(DADOS)
    dados["hotspot"].update(
        name={"pt": "Lago Guaíba, RS", "en": "Guaíba Lake, RS (Brazil)"},
        dominant_class="flood", confidence=0.86, baseline_pass="2026-01-14", threshold_db=3,
    )
    dados["hotspot"].update(campos)
    return dados


def corrigir(
    monkeypatch: pytest.MonkeyPatch, saidas: list[dict], dados: dict, idioma: str = "pt",
) -> tuple[Any, str]:
    """Roda o pedido com as saídas dadas; devolve a resposta e a mensagem de correção."""
    modelo = ModeloProgramado([[s] for s in saidas])
    _, prefetch = configurar(monkeypatch, modelo, [])
    prefetch.return_value = dados
    pedido = PEDIDO.model_copy(update={"language": idioma})
    resposta, _ = asyncio.run(modulo_agente.responder(pedido, carregar_config(), validar=validar_resposta))
    assert modelo.roteiro == []
    textos = [b["text"] for m in modelo.recebidas[-1] if m["role"] == "user"
              for b in m["content"] if "text" in b]
    return resposta, textos[-1]


@pytest.mark.parametrize(
    ("idioma", "linha"),
    [
        ("pt", "Mantenha os valores confirmados: 212 km², 86%, 14 jan 2026, 12 nov 2026, 3 dB."),
        ("en", "Mantenha os valores confirmados: 212 km²; 86%; Jan 14, 2026; Nov 12, 2026; 3 dB."),
    ],
)
def test_correcao_lista_os_valores_confirmados_a_manter(
    monkeypatch: pytest.MonkeyPatch, idioma: str, linha: str,
) -> None:
    _, correcao = corrigir(monkeypatch, [
        saida("inventada", "Desde 2024 o radar mediu **999 km²**."),
        saida("corrigida", "O radar mediu **212 km²**."),
    ], medido(), idioma)

    assert "o valor 999 não está nos resultados" in correcao
    assert linha in correcao.splitlines()


def test_correcao_sem_medicao_nao_pede_para_manter_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    sem_medicao = medido(affected_area_km2=None, confidence=None, last_pass=None,
                         baseline_pass=None, threshold_db=None)

    _, correcao = corrigir(monkeypatch, [
        saida("inventada", "O radar mediu **999 km²**."),
        saida("corrigida", "O radar ainda não tem medição."),
    ], sem_medicao)

    assert "Mantenha" not in correcao


def test_correcao_que_some_com_a_area_vira_resposta_segura(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="cass.agente")

    resposta, _ = corrigir(monkeypatch, [
        saida("inventada", "O radar mediu **999 km²**."),
        saida("corrigida-sem-area", "O radar mediu uma mudança de cheia no hotspot."),
    ], medido())

    assert resposta.texto.startswith(
        "Entre 14 jan 2026 e 12 nov 2026, o radar mediu **212 km²** de cheia em Lago Guaíba, RS."
    )
    assert resposta.confianca == "baixa"
    assert any("sem a área afetada" in r.getMessage() for r in caplog.records)
    assert not any("cheia no hotspot" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("saidas", "dados", "texto"),
    [
        # A correção cita a área: vale a resposta do modelo.
        ([saida("inventada", "O radar mediu **999 km²**."), saida("com-area", "O radar mediu **212 km²**.")],
         medido(), "O radar mediu **212 km²**."),
        # A área é null nos resultados: não há o que citar.
        ([saida("inventada", "O radar mediu **999 km²**."), saida("sem-area", "Ainda não há área medida.")],
         medido(affected_area_km2=None), "Ainda não há área medida."),
        # A regra só vale para a resposta corrigida.
        ([saida("primeira", "A classe dominante é cheia.")], medido(), "A classe dominante é cheia."),
    ],
)
def test_resposta_do_modelo_e_mantida_fora_do_caso_da_area(
    monkeypatch: pytest.MonkeyPatch, saidas: list[dict], dados: dict, texto: str,
) -> None:
    resposta, _ = corrigir(monkeypatch, saidas, dados)

    assert resposta.texto == texto


def test_pergunta_de_volta_corrigida_nao_precisa_da_area(monkeypatch: pytest.MonkeyPatch) -> None:
    pergunta = chamada(
        "RespostaDoModelo", "pergunta", tipo="pergunta_de_volta", texto="Qual hotspot você quer ver?",
        confianca="baixa", opcoes=["Lago Guaíba, RS"],
    )

    resposta, _ = corrigir(monkeypatch, [saida("inventada", "O radar mediu **999 km²**."), pergunta], medido())

    assert resposta.tipo == "pergunta_de_volta"


def test_sem_schema_valido_para_no_limite_e_endpoint_devolve_500(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ids_conhecidos: list[str],
) -> None:
    # Cada chamada à ferramenta de saída falha por faltar tipo e confiança.
    modelo = ModeloProgramado([
        [chamada("RespostaDoModelo", f"schema-invalido-{i}", texto="Sem os campos obrigatórios.")]
        for i in range(3)
    ])
    criados, prefetch = configurar(monkeypatch, modelo, [])

    with TestClient(main.app) as cliente:
        http = cliente.post("/api/agente", json=PEDIDO.model_dump())

    assert http.status_code == 500
    corpo = RespostaAgente.model_validate(http.json())
    assert corpo.tools == [] and corpo.sources == []
    assert len(criados) == 1
    prefetch.assert_awaited_once_with("guaiba-rs")
    assert modelo.roteiro == [] and len(modelo.recebidas) == 3
    assert criados[0]["steering"].correcoes_causalidade == 0
    assert criados[0]["portao"].chamadas == 1
    linhas = capsys.readouterr().out.splitlines()
    assert len(linhas) == 1 and json.loads(linhas[0])["tipo"] == "erro"


def test_numero_em_erro_de_ferramenta_nao_confirma_uma_medicao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool
    async def consultar_hotspot(id: str) -> dict:
        """Simula erro de execução contendo um número que não foi medido."""
        raise RuntimeError("Não foi possível confirmar os 999 km² solicitados.")

    modelo = ModeloProgramado([
        [chamada("consultar_hotspot", "consulta-com-erro", id="guaiba-rs")],
        [saida("numero-da-mensagem-de-erro", "O radar mediu 999 km².")],
        [saida("resposta-corrigida", "Não há dados suficientes para confirmar a área.")],
    ])
    criados, _ = configurar(monkeypatch, modelo, [consultar_hotspot])

    resposta, _ = asyncio.run(modulo_agente.responder(
        PEDIDO, carregar_config(), validar=validar_resposta,
    ))

    assert modelo.roteiro == [] and len(modelo.recebidas) == 3
    # A correção passou sem citar os 212 km² do hotspot aberto: vale a resposta segura.
    assert resposta.texto.startswith("Na passagem de 12 nov 2026, o radar mediu **212 km²**")
    assert "999" not in resposta.texto
    extraidos = modulo_agente._resultados_das_ferramentas(criados[0]["agente"].messages)
    assert extraidos == []


# ---------- limite de tokens de saída ----------


class ModeloQueEstoura(ModeloProgramado):
    """Um passo "max_tokens" no roteiro corta a resposta no teto de tokens de saída."""

    async def stream(self, messages: Any, *args: Any, **kwargs: Any):
        if not self.roteiro or self.roteiro[0] != "max_tokens":
            async for evento in super().stream(messages, *args, **kwargs):
                yield evento
            return
        self.recebidas.append(deepcopy(messages))
        self.roteiro.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Texto cortado pelo limite"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "max_tokens"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}


INICIO_SEGURO = "Entre 14 jan 2026 e 12 nov 2026, o radar mediu **212 km²** de cheia em Lago Guaíba, RS."


def test_limite_de_tokens_com_hotspot_aberto_vira_resposta_segura(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level("WARNING", logger="cass.agente")
    modelo = ModeloQueEstoura(["max_tokens"])
    _, prefetch = configurar(monkeypatch, modelo, [])
    prefetch.return_value = medido()

    resposta, ferramentas = asyncio.run(modulo_agente.responder(
        PEDIDO, carregar_config(), validar=validar_resposta,
    ))

    assert modelo.roteiro == []
    assert resposta.texto.startswith(INICIO_SEGURO)
    assert resposta.confianca == "baixa" and resposta.dados_de_exemplo is True
    assert ferramentas == ["consultar_hotspot"]
    publica = para_contrato(resposta, ferramentas)
    assert publica.sources == ["NISAR, 12 nov 2026"]
    assert [r.getMessage() for r in caplog.records] == ["limite de tokens; resposta segura"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["tipo"] == "resposta"


def test_limite_de_tokens_usa_o_hotspot_que_o_modelo_consultou(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool
    async def consultar_hotspot(id: str) -> dict:
        """Medição local do Guaíba."""
        return medido()

    modelo = ModeloQueEstoura([[chamada("consultar_hotspot", "consulta", id="guaiba-rs")], "max_tokens"])
    _, prefetch = configurar(monkeypatch, modelo, [consultar_hotspot])
    prefetch.return_value = None  # nada antecipado: o hotspot vem só da ferramenta

    resposta, ferramentas = asyncio.run(modulo_agente.responder(
        PEDIDO, carregar_config(), validar=validar_resposta,
    ))

    assert modelo.roteiro == []
    assert resposta.texto.startswith(INICIO_SEGURO)
    assert ferramentas == ["consultar_hotspot"]


def test_limite_de_tokens_na_correcao_tambem_vira_resposta_segura(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modelo = ModeloQueEstoura([[saida("inventada", "O radar mediu **999 km²**.")], "max_tokens"])
    _, prefetch = configurar(monkeypatch, modelo, [])
    prefetch.return_value = medido()

    resposta, _ = asyncio.run(modulo_agente.responder(PEDIDO, carregar_config(), validar=validar_resposta))

    assert modelo.roteiro == []
    assert resposta.texto.startswith(INICIO_SEGURO)
    assert "999" not in resposta.model_dump_json()


def test_limite_de_tokens_sem_hotspot_segue_como_erro(monkeypatch: pytest.MonkeyPatch) -> None:
    modelo = ModeloQueEstoura(["max_tokens"])
    _, prefetch = configurar(monkeypatch, modelo, [])
    prefetch.return_value = None

    with pytest.raises(MaxTokensReachedException):
        asyncio.run(modulo_agente.responder(PEDIDO, carregar_config(), validar=validar_resposta))


@pytest.mark.parametrize(("antecipado", "status"), [(True, 200), (False, 500)])
def test_endpoint_no_limite_de_tokens(
    monkeypatch: pytest.MonkeyPatch, ids_conhecidos: list[str], antecipado: bool, status: int,
) -> None:
    modelo = ModeloQueEstoura(["max_tokens"])
    _, prefetch = configurar(monkeypatch, modelo, [])
    prefetch.return_value = medido() if antecipado else None

    with TestClient(main.app) as cliente:
        http = cliente.post("/api/agente", json=PEDIDO.model_dump())

    assert http.status_code == status
    corpo = RespostaAgente.model_validate(http.json())
    if antecipado:
        assert corpo.paragraphs[0].startswith(INICIO_SEGURO)
        assert "Texto cortado" not in http.text


# ---------- tempo limite ----------


class ModeloQueDemora(ModeloProgramado):
    """Um passo "demora" no roteiro segura a chamada ao modelo até o prazo acabar."""

    async def stream(self, messages: Any, *args: Any, **kwargs: Any):
        if self.roteiro and self.roteiro[0] == "demora":
            self.recebidas.append(deepcopy(messages))
            self.roteiro.pop(0)
            await asyncio.sleep(30)
        async for evento in super().stream(messages, *args, **kwargs):
            yield evento


def prazo_curto(monkeypatch: pytest.MonkeyPatch, *modulos: Any, segundos: float = 0.3) -> None:
    for modulo in modulos:
        monkeypatch.setattr(modulo, "TEMPO_LIMITE_S", segundos)


def test_tempo_limite_com_hotspot_aberto_vira_resposta_segura(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="cass.agente")
    _, prefetch = configurar(monkeypatch, ModeloQueDemora(["demora"]), [])
    prefetch.return_value = medido()
    prazo_curto(monkeypatch, modulo_agente)

    inicio = time.monotonic()
    resposta, ferramentas = asyncio.run(modulo_agente.responder(
        PEDIDO, carregar_config(), validar=validar_resposta,
    ))

    assert time.monotonic() - inicio < 2
    assert resposta.texto.startswith(INICIO_SEGURO)
    assert resposta.confianca == "baixa" and resposta.dados_de_exemplo is True
    assert ferramentas == ["consultar_hotspot"]
    assert [r.getMessage() for r in caplog.records] == ["tempo limite; resposta segura"]


def test_tempo_limite_usa_o_hotspot_que_o_modelo_consultou(monkeypatch: pytest.MonkeyPatch) -> None:
    @tool
    async def consultar_hotspot(id: str) -> dict:
        """Medição local do Guaíba."""
        return medido()

    modelo = ModeloQueDemora([[chamada("consultar_hotspot", "consulta", id="guaiba-rs")], "demora"])
    _, prefetch = configurar(monkeypatch, modelo, [consultar_hotspot])
    prefetch.return_value = None
    prazo_curto(monkeypatch, modulo_agente)

    resposta, ferramentas = asyncio.run(modulo_agente.responder(
        PEDIDO, carregar_config(), validar=validar_resposta,
    ))

    assert resposta.texto.startswith(INICIO_SEGURO)
    assert ferramentas == ["consultar_hotspot"]


def test_tempo_limite_na_correcao_tambem_vira_resposta_segura(monkeypatch: pytest.MonkeyPatch) -> None:
    modelo = ModeloQueDemora([[saida("inventada", "O radar mediu **999 km²**.")], "demora"])
    _, prefetch = configurar(monkeypatch, modelo, [])
    prefetch.return_value = medido()
    prazo_curto(monkeypatch, modulo_agente)

    resposta, _ = asyncio.run(modulo_agente.responder(PEDIDO, carregar_config(), validar=validar_resposta))

    assert resposta.texto.startswith(INICIO_SEGURO)
    assert "999" not in resposta.model_dump_json()


def test_tempo_limite_sem_hotspot_segue_como_agente_demorou(monkeypatch: pytest.MonkeyPatch) -> None:
    _, prefetch = configurar(monkeypatch, ModeloQueDemora(["demora"]), [])
    prefetch.return_value = None
    prazo_curto(monkeypatch, modulo_agente)

    with pytest.raises(modulo_agente.AgenteDemorou):
        asyncio.run(modulo_agente.responder(PEDIDO, carregar_config(), validar=validar_resposta))


@pytest.mark.parametrize(("antecipado", "status"), [(True, 200), (False, 504)])
def test_endpoint_no_tempo_limite(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, ids_conhecidos: list[str],
    antecipado: bool, status: int,
) -> None:
    _, prefetch = configurar(monkeypatch, ModeloQueDemora(["demora"]), [])
    prefetch.return_value = medido() if antecipado else None
    # Como em produção: a mesma constante nos dois módulos; o prazo do endpoint,
    # que começa antes, é o que vence.
    prazo_curto(monkeypatch, modulo_agente, main)

    with caplog.at_level("WARNING"), TestClient(main.app) as cliente:
        http = cliente.post("/api/agente", json=PEDIDO.model_dump())

    assert http.status_code == status
    corpo = RespostaAgente.model_validate(http.json())
    if antecipado:
        assert corpo.paragraphs[0].startswith(INICIO_SEGURO)
        assert corpo.tools == ["consultar_hotspot"]
        assert corpo.sources == ["NISAR, 12 nov 2026"]
        assert "tempo limite; resposta segura" in caplog.text
    else:
        assert "o agente passou do tempo limite" in caplog.text
        assert "resposta segura" not in caplog.text


@pytest.mark.parametrize("antecipado", [True, False])
def test_stream_no_tempo_limite(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, ids_conhecidos: list[str],
    antecipado: bool,
) -> None:
    _, prefetch = configurar(monkeypatch, ModeloQueDemora(["demora"]), [])
    prefetch.return_value = medido() if antecipado else None
    prazo_curto(monkeypatch, modulo_agente, main)

    with caplog.at_level("WARNING"), TestClient(main.app) as cliente:
        http = cliente.post("/api/agente/stream", json=PEDIDO.model_dump())

    assert http.status_code == 200
    nome, dados = _eventos(http.text)[-1]
    if antecipado:
        assert nome == "resposta"
        corpo = RespostaAgente.model_validate(dados)
        assert corpo.paragraphs[0].startswith(INICIO_SEGURO)
        assert "tempo limite; resposta segura" in caplog.text
    else:
        assert nome == "erro" and dados["status"] == 504
        assert "resposta segura" not in caplog.text
