"""Comportamento do entrypoint, sem Docker, rede ou processos do serviço."""

import os
import subprocess
from pathlib import Path

import pytest

START = Path(__file__).resolve().parent.parent / "start.sh"


@pytest.mark.parametrize(
    ("porta", "esperada"),
    [(None, "8080"), ("", "8080"), ("9091", "9091")],
    ids=["porta-ausente", "porta-vazia", "porta-configurada"],
)
def test_start_encaminha_porta_argumentos_pid_e_codigo_de_saida(
    tmp_path: Path, porta: str | None, esperada: str,
) -> None:
    uvicorn = tmp_path / "uvicorn"
    uvicorn.write_text("#!/bin/sh\nprintf '%s\\n' \"$$\" \"$@\"\nexit 37\n", encoding="utf-8")
    uvicorn.chmod(0o755)
    ambiente = os.environ.copy()
    ambiente["DECIDER_ATIVO"] = "false"
    ambiente["DECIDER_INCLUIDO"] = "true"
    ambiente["PATH"] = f"{tmp_path}{os.pathsep}{ambiente.get('PATH', os.defpath)}"
    if porta is None:
        ambiente.pop("PORT", None)
    else:
        ambiente["PORT"] = porta

    processo = subprocess.Popen(
        ["/bin/sh", str(START)], env=ambiente,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        saida, erros = processo.communicate(timeout=5)
    finally:
        if processo.poll() is None:
            processo.kill()
            processo.wait()

    linhas = saida.splitlines()
    assert erros == ""
    assert processo.returncode == 37
    # O exec mantém o PID do entrypoint, em vez de deixar um shell intermediário.
    assert linhas[0] == str(processo.pid)
    assert linhas[1:] == ["cass.main:app", "--host", "0.0.0.0", "--port", esperada, "--no-proxy-headers"]


@pytest.mark.parametrize("ativo", ["true", " TRUE ", "1", "Sim", "yes"])
def test_start_ativo_entrega_o_pid_ao_supervisor(tmp_path: Path, ativo: str) -> None:
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nprintf '%s\\n' \"$$\" \"$@\"\nexit 37\n", encoding="utf-8")
    python.chmod(0o755)
    ambiente = {**os.environ, "DECIDER_ATIVO": ativo, "PATH": f"{tmp_path}{os.pathsep}{os.defpath}"}

    processo = subprocess.Popen(
        ["/bin/sh", str(START)], env=ambiente,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        saida, erros = processo.communicate(timeout=5)
    finally:
        if processo.poll() is None:
            processo.kill()
            processo.wait()

    assert erros == "" and processo.returncode == 37
    assert saida.splitlines() == [str(processo.pid), "-m", "cass.inicializacao"]
