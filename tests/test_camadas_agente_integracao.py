"""Camadas de proteção com Agent real, mantendo a mesma cota entre correções."""

import asyncio
import json
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from strands import tool

from cass import agente as modulo_agente
from cass import main
from cass.config import carregar_config
from cass.contrato import para_contrato
from cass.hooks import ProgressoERegistro
from cass.modelos import PedidoAgente, RespostaAgente
from cass.verificacao import afirma_causalidade, validar_resposta
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
    assert resposta.texto == "Não há dados suficientes para confirmar a área."
    extraidos = modulo_agente._resultados_das_ferramentas(criados[0]["agente"].messages)
    assert extraidos == []
