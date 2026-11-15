"""Inicia o Decider CPU e supervisiona os dois processos do container.

O caminho sem Decider continua no exec direto de start.sh. Este módulo não
importa PyTorch: somente o processo filho do Decider carrega os pesos locais.
"""

import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterable
from types import FrameType
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

PORTA_DECIDER = 8099
MODELO_PADRAO = "StrandsAgents/strands-decider-2B-qwen3.5-v1-2610"
INTERVALO_S = 0.1


def _prazo(nome: str, padrao: float) -> float:
    texto = os.environ.get(nome, "").strip()
    valor = float(texto) if texto else padrao
    if not math.isfinite(valor) or valor <= 0:
        raise ValueError(f"{nome} deve ser um número positivo e finito")
    return valor


def _codigo_saida(codigo: int) -> int:
    # Um servidor que termina espontaneamente com zero também interrompe o serviço.
    return codigo if codigo > 0 else 128 - codigo if codigo < 0 else 1


def _sinalizar(processo: subprocess.Popen, sinal: int) -> None:
    try:
        # Cada filho é líder de uma sessão própria; inclui eventuais descendentes.
        os.killpg(processo.pid, sinal)
    except ProcessLookupError:
        pass


def _encerrar(processos: Iterable[subprocess.Popen], sinal: int, timeout_s: float) -> None:
    processos = tuple(processos)
    for processo in processos:
        _sinalizar(processo, sinal)
    fim = time.monotonic() + timeout_s
    for processo in processos:
        try:
            processo.wait(timeout=max(0.0, fim - time.monotonic()))
        except subprocess.TimeoutExpired:
            _sinalizar(processo, signal.SIGKILL)
            processo.wait()


def _porta_disponivel() -> None:
    # Falha antes de carregar outra cópia dos pesos caso a porta já esteja ocupada.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserva:
        reserva.bind(("127.0.0.1", PORTA_DECIDER))


def _saudavel(modelo: str, timeout_s: float) -> bool:
    # O tráfego local não depende de HTTP_PROXY/HTTPS_PROXY do ambiente.
    cliente = build_opener(ProxyHandler({}))
    try:
        with cliente.open(f"http://127.0.0.1:{PORTA_DECIDER}/health", timeout=timeout_s) as resposta:
            if resposta.status != 200:
                return False
            dados = json.loads(resposta.read(65_536))
        return (
            isinstance(dados, dict)
            and dados.get("status") == "ok"
            and dados.get("checkpoint") == modelo
            and dados.get("device") == "cpu"
        )
    except (OSError, URLError, ValueError):
        return False


def main() -> int:
    """Só expõe o Cass após o Decider carregar e aquecer; encerra os dois juntos."""
    if os.environ.get("DECIDER_INCLUIDO", "false").strip().lower() != "true":
        print(
            "DECIDER_ATIVO exige uma imagem construída com INCLUIR_DECIDER=true.",
            file=sys.stderr, flush=True,
        )
        return 2

    processos: list[subprocess.Popen] = []
    recebido: int | None = None
    anteriores: dict[int, object] = {}
    timeout_encerramento = 10.0

    def receber(sinal: int, quadro: FrameType | None) -> None:
        nonlocal recebido
        recebido = recebido or sinal
        for processo in tuple(processos):
            _sinalizar(processo, sinal)

    try:
        timeout_inicio = _prazo("DECIDER_STARTUP_TIMEOUT_S", 240.0)
        timeout_encerramento = _prazo("DECIDER_SHUTDOWN_TIMEOUT_S", 10.0)
        modelo = os.environ.get("DECIDER_MODELO", "").strip() or MODELO_PADRAO
        _porta_disponivel()
        for sinal in (signal.SIGTERM, signal.SIGINT):
            anteriores[sinal] = signal.signal(sinal, receber)

        ambiente_decider = {
            **os.environ,
            "HF_HOME": os.environ.get("HF_HOME", "").strip() or "/opt/decider-cache",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
        decider = subprocess.Popen(
            [
                "strands-decider", "serve", modelo,
                "--host", "127.0.0.1", "--port", str(PORTA_DECIDER),
                "--device", "cpu", "--max-concurrency", "1", "--max-batch", "1", "--warmup",
            ],
            env=ambiente_decider, start_new_session=True,
        )
        processos.append(decider)
        prazo = time.monotonic() + timeout_inicio
        while not recebido:
            if (codigo := decider.poll()) is not None:
                print("O Decider terminou antes de ficar pronto.", file=sys.stderr, flush=True)
                return _codigo_saida(codigo)
            restante = prazo - time.monotonic()
            if restante <= 0:
                raise TimeoutError("O Decider não ficou pronto no prazo de inicialização.")
            if _saudavel(modelo, min(1.0, restante)):
                break
            time.sleep(min(INTERVALO_S, restante))
        if recebido:
            return 128 + recebido

        # Conferência após o health check evita iniciar o Cass se o filho já morreu.
        if (codigo := decider.poll()) is not None:
            return _codigo_saida(codigo)
        porta = os.environ.get("PORT", "").strip() or "8080"
        cass = subprocess.Popen(
            ["uvicorn", "cass.main:app", "--host", "0.0.0.0", "--port", porta],
            start_new_session=True,
        )
        processos.append(cass)
        while not recebido:
            for processo in processos:
                if (codigo := processo.poll()) is not None:
                    return _codigo_saida(codigo)
            time.sleep(INTERVALO_S)
        return 128 + recebido
    except (OSError, ValueError, TimeoutError) as erro:
        print(f"Falha na inicialização do Cass com Decider: {erro}", file=sys.stderr, flush=True)
        return 1
    finally:
        _encerrar(processos, recebido or signal.SIGTERM, timeout_encerramento)
        for sinal, anterior in anteriores.items():
            signal.signal(sinal, anterior)


if __name__ == "__main__":
    raise SystemExit(main())
