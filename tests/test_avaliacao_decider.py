"""Testes das ferramentas de avaliação; HTTP simulado só nestes testes unitários."""

import hashlib
import asyncio
import json
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scripts import avaliar_decider as avaliacao


def exemplo(tarefa: str = "scope", label: int = 1, estado: str = "Radar.") -> dict:
    pergunta = avaliacao.TAREFAS[tarefa].get(
        "instructions", "O usuário deixou claro que quer o hotspot guaiba-rs?"
    )
    return {"kind": "noul", "state": estado, "instructions": pergunta,
            "options": [["false", ""], ["true", ""]], "label": label, "task": tarefa}


def test_piloto_esta_congelado_balanceado_e_no_formato_upstream() -> None:
    linhas, digest = avaliacao.carregar_dataset(avaliacao.DATASET_PADRAO)

    assert 30 <= len(linhas) <= 60
    assert len(linhas) == 36
    assert digest == hashlib.sha256(avaliacao.DATASET_PADRAO.read_bytes()).hexdigest()
    assert Counter(e["task"] for e in linhas) == {"scope": 12, "injection": 12, "clear_hotspot": 12}
    for tarefa in avaliacao.TAREFAS:
        assert Counter(e["label"] for e in linhas if e["task"] == tarefa) == {0: 6, 1: 6}
    assert all(set(e) == {"kind", "state", "instructions", "options", "label", "task"} for e in linhas)
    manifesto = json.loads((avaliacao.RAIZ / "evaluation" / "decider" / "manifest.json").read_text())
    assert manifesto["label_audit"] == "not_done"
    assert manifesto["calibration_or_training"] == "not_done"


def test_perguntas_estao_sincronizadas_com_o_cass() -> None:
    from cass.guardrails import PERGUNTA_ESCOPO, PERGUNTA_INJECAO

    assert avaliacao.TAREFAS["scope"]["instructions"] == PERGUNTA_ESCOPO
    assert avaliacao.TAREFAS["injection"]["instructions"] == PERGUNTA_INJECAO


@pytest.mark.parametrize("alteracao", [
    {"kind": "choice"}, {"label": True}, {"label": 2}, {"state": ""},
    {"options": [["true", ""], ["false", ""]]}, {"task": "desconhecida"},
    {"instructions": "Outra pergunta que mudaria a comparação."},
])
def test_linha_invalida_nao_chega_a_consultar_modelo(tmp_path: Path, alteracao: dict) -> None:
    caminho = tmp_path / "piloto.jsonl"
    caminho.write_text(json.dumps({**exemplo(), **alteracao}) + "\n")

    with pytest.raises(ValueError):
        avaliacao.carregar_dataset(caminho)


@pytest.mark.parametrize(
    ("tarefa", "score", "predicao"),
    [("scope", 0.3, 1), ("scope", 0.2999, 0), ("injection", 0.7, 0),
     ("injection", 0.7001, 1), ("clear_hotspot", 0.5, 1), ("clear_hotspot", 0.4999, 0)],
)
def test_predicao_respeita_limiares_e_comparadores(tarefa: str, score: float, predicao: int) -> None:
    assert avaliacao.prever(tarefa, score) == predicao


def test_consulta_envia_payload_real_sem_criterios_diferentes() -> None:
    recebidos = []

    def tratar(request: httpx.Request) -> httpx.Response:
        recebidos.append(request)
        return httpx.Response(200, json={"answers": {"q": {"noul": 0.9}}})

    with httpx.Client(transport=httpx.MockTransport(tratar)) as cliente:
        registro = avaliacao.consultar(cliente, "http://decider.teste/v1/systemone", exemplo(), 1.5)

    assert registro["status"] == "ok" and registro["score"] == 0.9 and registro["predicao"] == 1
    assert registro["latencia_ms"] >= 0
    assert json.loads(recebidos[0].content) == {
        "state": "Radar.", "questions": {"q": {
            "type": "noul", "instructions": avaliacao.TAREFAS["scope"]["instructions"],
        }},
    }
    assert set(recebidos[0].extensions["timeout"].values()) == {1.5}


@pytest.mark.parametrize("valor", [True, None, "0.9", {}, [], -0.1, 1.1, float("nan")])
def test_valor_invalido_nao_vira_score_ou_acerto_por_padrao(valor: Any) -> None:
    transporte = httpx.MockTransport(lambda pedido: httpx.Response(
        200, content=json.dumps({"answers": {"q": {"noul": valor}}}),
    ))
    with httpx.Client(transport=transporte) as cliente:
        registro = avaliacao.consultar(cliente, "http://decider.teste", exemplo(), 1.5)

    assert registro["status"] == "resposta_invalida"
    assert registro["score"] is None and registro["predicao"] is None


@pytest.mark.parametrize("falha,status", [
    (httpx.ReadTimeout("lento"), "timeout"),
    (httpx.ConnectError("indisponível"), "erro_transporte"),
    (httpx.Response(503), "http_error"),
    (httpx.Response(200, text="não JSON"), "resposta_invalida"),
    (httpx.Response(200, json={}), "resposta_invalida"),
])
def test_falhas_ficam_explicitas_em_vez_de_fallback(falha: Any, status: str) -> None:
    def tratar(pedido: httpx.Request) -> httpx.Response:
        if isinstance(falha, Exception):
            raise falha
        return falha

    with httpx.Client(transport=httpx.MockTransport(tratar)) as cliente:
        registro = avaliacao.consultar(cliente, "http://decider.teste", exemplo(), 1.5)

    assert registro["status"] == status
    assert registro["predicao"] is None and registro["score"] is None


def test_metricas_separam_acuracia_real_de_politica_com_fallback() -> None:
    registros = [
        {"label": 1, "predicao": 1, "predicao_com_padrao": 1, "status": "ok", "latencia_ms": 20},
        {"label": 1, "predicao": None, "predicao_com_padrao": 1, "status": "timeout", "latencia_ms": 1500},
    ]

    resumo = avaliacao.resumir(registros)

    assert resumo["acuracia_todos"] == 0.5
    assert resumo["acuracia_validos"] == 1.0
    assert resumo["acuracia_politica_com_padrao"] == 1.0
    assert resumo["cobertura"] == 0.5
    assert resumo["respostas_ausentes"] == 1
    assert resumo["latencia_todas"]["p95_ms"] == 1500
    assert resumo["latencia_validas"]["p95_ms"] == 20


def test_wilson_e_percentil_sao_definidos_para_poucos_itens() -> None:
    assert avaliacao.intervalo_wilson(0, 0) is None
    assert avaliacao.latencias([])["mediana_ms"] is None
    assert avaliacao.latencias([7])["p95_ms"] == 7
    inferior, superior = avaliacao.intervalo_wilson(0, 20)
    assert inferior == pytest.approx(0)
    assert superior == pytest.approx(0.161125, abs=0.00001)
    assert avaliacao.latencias(list(range(1, 21)))["p95_ms"] == 19


def test_comparacao_pareada_nao_conta_timeout_como_vitoria() -> None:
    registros = [
        {"linha": 1, "repeticao": 0, "modo": "false", "label": 1, "predicao": 1},
        {"linha": 1, "repeticao": 0, "modo": "true", "label": 1, "predicao": None},
        {"linha": 2, "repeticao": 0, "modo": "false", "label": 1, "predicao": 0},
        {"linha": 2, "repeticao": 0, "modo": "true", "label": 1, "predicao": 1},
    ]

    pares = avaliacao.comparar_pares(registros)

    assert pares["total"] == 2 and pares["pares_validos"] == 1
    assert pares["pares_incompletos"] == 1 and pares["so_modelo_certo"] == 1
    assert pares["so_padrao_certo"] == 0


def test_piloto_direto_intercala_variantes_e_marca_medicao_incompleta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas = []
    original_client = httpx.Client

    def tratar(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        estado = json.loads(request.content)["state"]
        return httpx.Response(200, json={"answers": {"q": {"noul": 0.9}}}) if estado == "válido" else httpx.Response(503)

    monkeypatch.setattr(avaliacao.httpx, "Client", lambda: original_client(transport=httpx.MockTransport(tratar)))
    exemplos = [exemplo(estado="válido"), exemplo(label=0, estado="falha")]

    relatorio = avaliacao.avaliar_direto(exemplos, "http://decider.teste", 1.5, 2, aquecimento=0)

    assert len(chamadas) == 4
    assert len(relatorio["registros"]) == 8
    assert relatorio["completo"] is False
    assert relatorio["resumos"]["true"]["overall"]["cobertura"] == 0.5
    assert relatorio["resumos"]["false"]["overall"]["acuracia_todos"] == 0.5
    assert [r["modo"] for r in relatorio["registros"][:4]] == ["false", "true", "true", "false"]


def test_cli_salva_relatorio_incompleto_e_nao_chama_gemini_sem_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    saida = tmp_path / "relatorio.json"
    monkeypatch.setattr(avaliacao, "avaliar_direto", lambda *args: {"completo": False})
    monkeypatch.setattr(avaliacao, "avaliar_gemini", lambda *args: pytest.fail("Gemini não foi solicitado"))

    codigo = avaliacao.main(["--output", str(saida), "--aquecimento", "0"])

    assert codigo == 2
    relatorio = json.loads(saida.read_text())
    assert relatorio["tipo"] == "piloto_sintetico_nao_auditado"
    assert relatorio["modelo_servido_confirmado"] is False
    assert "gemini" not in relatorio
    assert relatorio["dataset"]["itens"] == 36


def test_cli_gemini_so_executa_com_flag_explicita(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas = []
    monkeypatch.setattr(avaliacao, "avaliar_direto", lambda *args: {"completo": True})

    def avaliar(*args: Any) -> dict:
        chamadas.append(args)
        return {"completo": False}

    monkeypatch.setattr(avaliacao, "avaliar_gemini", avaliar)

    assert avaliacao.main(["--output", str(tmp_path / "relatorio.json"), "--gemini"]) == 2
    assert chamadas == [(avaliacao.URL_PADRAO, 1)]


def test_bateria_preserva_loop_do_cliente_cacheado_entre_casos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduz a restrição de um transporte assíncrono preso ao primeiro loop."""
    import fastapi.testclient
    from cass import main

    loop_do_modelo = None
    chamadas = []

    async def responder(*args: Any, **kwargs: Any) -> tuple:
        nonlocal loop_do_modelo
        atual = asyncio.get_running_loop()
        if loop_do_modelo is None:
            loop_do_modelo = atual
        assert atual is loop_do_modelo, "O cliente cacheado perdeu seu event loop"
        chamadas.append(main.app.state.config.decider_ativo)
        return SimpleNamespace(tipo="resposta"), []

    class ClienteLocal:
        def __init__(self, app: Any) -> None:
            self.loop = asyncio.new_event_loop()

        def __enter__(self) -> "ClienteLocal":
            return self

        def __exit__(self, *args: Any) -> None:
            self.loop.close()

        def post(self, url: str, **kwargs: Any) -> httpx.Response:
            self.loop.run_until_complete(main._responder_protegido())
            return httpx.Response(200, json={
                "paragraphs": ["Resposta sintética."], "tools": [], "sources": [], "note": None,
            })

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "projeto-teste")
    monkeypatch.setattr(main, "app", SimpleNamespace(state=SimpleNamespace()))
    monkeypatch.setattr(main, "_responder_protegido", responder)
    monkeypatch.setattr(fastapi.testclient, "TestClient", ClienteLocal)
    monkeypatch.setattr(avaliacao, "servidor_dados", lambda: nullcontext("http://dados-teste.invalid"))
    monkeypatch.setattr(avaliacao, "conferir_caso", lambda *args: [])

    resultado = avaliacao.avaliar_gemini(avaliacao.URL_PADRAO, 1)

    assert len(chamadas) == 18 and chamadas.count(True) == chamadas.count(False) == 9
    assert resultado["completo"]
    assert all(r["acerto"] for r in resultado["registros"])


@pytest.mark.parametrize("argumento,valor", [("--timeout", "0"), ("--repeticoes", "0"), ("--aquecimento", "-1")])
def test_cli_rejeita_configuracao_invalida(tmp_path: Path, argumento: str, valor: str) -> None:
    with pytest.raises(SystemExit) as erro:
        avaliacao.main(["--output", str(tmp_path / "relatorio.json"), argumento, valor])
    assert erro.value.code == 2
