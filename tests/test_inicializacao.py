"""Supervisão de processos reais simulados, sem PyTorch, modelos ou rede externa."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from cass import inicializacao

RAIZ = Path(__file__).resolve().parent.parent
FILHO = r'''#!/usr/bin/env python3
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

papel = "decider" if Path(sys.argv[0]).name == "strands-decider" else "cass"

def registrar(evento, **campos):
    with open(os.environ["EVENTOS_TESTE"], "a", encoding="utf-8") as arquivo:
        arquivo.write(json.dumps({"evento": evento, "pid": os.getpid(), **campos}) + "\n")

def parar(sinal, quadro):
    registrar(papel + "-sinal", sinal=sinal)
    if papel == "decider" and os.environ.get("IGNORAR_TERM") == "1":
        return
    raise SystemExit(0)

signal.signal(signal.SIGTERM, parar)
signal.signal(signal.SIGINT, parar)
registrar(papel + "-iniciou", args=sys.argv[1:], hf_home=os.environ.get("HF_HOME"),
          offline=os.environ.get("HF_HUB_OFFLINE"), transformers_offline=os.environ.get("TRANSFORMERS_OFFLINE"))
if papel == "cass":
    if os.environ.get("SAIDA_CASS"):
        raise SystemExit(int(os.environ["SAIDA_CASS"]))
    while True:
        time.sleep(0.1)

if os.environ.get("SAIDA_ANTES_HEALTH"):
    raise SystemExit(int(os.environ["SAIDA_ANTES_HEALTH"]))
time.sleep(float(os.environ.get("ATRASO_HEALTH", "0")))
porta = int(sys.argv[sys.argv.index("--port") + 1])
modelo = sys.argv[2]

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "checkpoint": os.environ.get("HEALTH_MODELO", modelo),
                                    "device": os.environ.get("HEALTH_DEVICE", "cpu")}).encode())
        registrar("health-atendido")
    def log_message(self, *args):
        pass

servidor = HTTPServer(("127.0.0.1", porta), Health)
registrar("decider-pronto")
if os.environ.get("SAIDA_DECIDER"):
    def morrer():
        registrar("decider-morreu")
        os._exit(int(os.environ["SAIDA_DECIDER"]))
    threading.Timer(0.5, morrer).start()
servidor.serve_forever(poll_interval=0.05)
'''


def eventos(caminho: Path) -> list[dict]:
    if not caminho.exists():
        return []
    return [json.loads(linha) for linha in caminho.read_text().splitlines() if linha]


def esperar(caminho: Path, nome: str) -> dict:
    fim = time.monotonic() + 5
    while time.monotonic() < fim:
        for evento in eventos(caminho):
            if evento["evento"] == nome:
                return evento
        time.sleep(0.02)
    raise AssertionError(f"O processo não registrou {nome}: {eventos(caminho)}")


@pytest.fixture
def rodar(tmp_path: Path) -> Iterator[Callable[..., tuple[subprocess.Popen, Path]]]:
    for nome in ("strands-decider", "uvicorn"):
        executavel = tmp_path / nome
        executavel.write_text(FILHO, encoding="utf-8")
        executavel.chmod(0o755)
    iniciados: list[tuple[subprocess.Popen, Path]] = []

    def iniciar(porta: int | None = None, **variaveis: str) -> tuple[subprocess.Popen, Path]:
        if porta is None:
            with socket.socket() as reserva:
                reserva.bind(("127.0.0.1", 0))
                porta = reserva.getsockname()[1]
        caminho = tmp_path / f"eventos-{len(iniciados)}.jsonl"
        ambiente = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
            "DECIDER_INCLUIDO": "true", "DECIDER_ATIVO": "true",
            "DECIDER_MODELO": "modelo-local-de-teste", "PORT": "9191",
            "DECIDER_STARTUP_TIMEOUT_S": "3", "DECIDER_SHUTDOWN_TIMEOUT_S": "0.3",
            "EVENTOS_TESTE": str(caminho), "HF_HOME": "/cache-de-teste",
            **variaveis,
        }
        comando = (
            "from cass import inicializacao; "
            f"inicializacao.PORTA_DECIDER={porta}; "
            "raise SystemExit(inicializacao.main())"
        )
        processo = subprocess.Popen(
            [sys.executable, "-c", comando], cwd=RAIZ, env=ambiente,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        iniciados.append((processo, caminho))
        return processo, caminho

    yield iniciar
    for processo, caminho in iniciados:
        if processo.poll() is None:
            processo.terminate()
            try:
                processo.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                processo.kill()
                processo.communicate(timeout=3)
        for evento in eventos(caminho):
            if evento["evento"] in {"decider-iniciou", "cass-iniciou"}:
                try:
                    os.killpg(evento["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_imagem_sem_modelo_nao_inicia_decider(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DECIDER_INCLUIDO", "false")
    assert inicializacao.main() == 2
    assert "INCLUIR_DECIDER=true" in capsys.readouterr().err


def test_uvicorn_so_inicia_apos_health_cpu_com_modelo_correto(rodar: Callable) -> None:
    processo, caminho = rodar(ATRASO_HEALTH="0.15", SAIDA_CASS="37")

    saida, erros = processo.communicate(timeout=5)

    assert processo.returncode == 37 and saida == "" and erros == ""
    registrados = eventos(caminho)
    nomes = [e["evento"] for e in registrados]
    assert nomes.index("health-atendido") < nomes.index("cass-iniciou")
    decider = next(e for e in registrados if e["evento"] == "decider-iniciou")
    assert decider["args"] == [
        "serve", "modelo-local-de-teste", "--host", "127.0.0.1", "--port",
        decider["args"][5], "--device", "cpu", "--max-concurrency", "1", "--max-batch", "1", "--warmup",
    ]
    assert decider["offline"] == decider["transformers_offline"] == "1"
    assert decider["hf_home"] == "/cache-de-teste"
    cass = next(e for e in registrados if e["evento"] == "cass-iniciou")
    assert cass["args"] == ["cass.main:app", "--host", "0.0.0.0", "--port", "9191", "--no-proxy-headers"]
    assert "decider-sinal" in nomes


def test_decider_que_morre_derruba_uvicorn_e_preserva_codigo(rodar: Callable) -> None:
    processo, caminho = rodar(SAIDA_DECIDER="21")
    processo.communicate(timeout=5)

    assert processo.returncode == 21
    assert any(e["evento"] == "cass-sinal" for e in eventos(caminho))


def test_decider_que_falha_na_carga_nao_sobe_uvicorn(rodar: Callable) -> None:
    processo, caminho = rodar(SAIDA_ANTES_HEALTH="17")
    _, erros = processo.communicate(timeout=5)

    assert processo.returncode == 17
    assert "antes de ficar pronto" in erros
    assert not any(e["evento"] == "cass-iniciou" for e in eventos(caminho))


@pytest.mark.parametrize("sinal", [signal.SIGTERM, signal.SIGINT])
def test_sinal_do_container_chega_aos_dois_processos(rodar: Callable, sinal: int) -> None:
    processo, caminho = rodar()
    esperar(caminho, "cass-iniciou")

    processo.send_signal(sinal)
    processo.communicate(timeout=5)

    assert processo.returncode == 128 + sinal
    registrados = eventos(caminho)
    for nome in ("decider-sinal", "cass-sinal"):
        assert any(e["evento"] == nome and e["sinal"] == sinal for e in registrados)


@pytest.mark.parametrize("variaveis", [
    {"ATRASO_HEALTH": "3"}, {"HEALTH_MODELO": "outro-modelo"}, {"HEALTH_DEVICE": "cuda"},
])
def test_timeout_ou_health_incorreto_nao_expõe_o_cass(rodar: Callable, variaveis: dict) -> None:
    processo, caminho = rodar(DECIDER_STARTUP_TIMEOUT_S="0.35", **variaveis)
    _, erros = processo.communicate(timeout=5)

    assert processo.returncode == 1
    assert "prazo de inicialização" in erros
    assert not any(e["evento"] == "cass-iniciou" for e in eventos(caminho))


def test_porta_ocupada_falha_antes_de_carregar_outra_copia_do_modelo(rodar: Callable) -> None:
    with socket.socket() as reserva:
        reserva.bind(("127.0.0.1", 0))
        reserva.listen()
        processo, caminho = rodar(porta=reserva.getsockname()[1])
        _, erros = processo.communicate(timeout=5)

    assert processo.returncode == 1
    assert "Falha na inicialização" in erros
    assert eventos(caminho) == []


def test_processo_que_ignora_encerramento_e_morto_e_recolhido(rodar: Callable) -> None:
    processo, caminho = rodar(IGNORAR_TERM="1", SAIDA_CASS="37")
    processo.communicate(timeout=5)
    decider = esperar(caminho, "decider-iniciou")

    assert processo.returncode == 37
    with pytest.raises(ProcessLookupError):
        os.kill(decider["pid"], 0)
