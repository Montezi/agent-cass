"""Empacotamento do checkpoint e da base, sem rede nem bibliotecas de ML."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.baixar_modelo_decider import (
    ARQUIVOS_CHECKPOINT, BASE, MODELO, REVISAO, REVISAO_BASE, preparar_modelo,
)


@pytest.fixture
def snapshots(tmp_path: Path) -> tuple[Path, Path, Mock]:
    checkpoint, base = tmp_path / "checkpoint", tmp_path / "base"
    (checkpoint / "lora").mkdir(parents=True)
    base.mkdir()
    (checkpoint / "strands_decider_config.json").write_text(json.dumps({
        "base_model": BASE, "use_lora": True,
    }))
    (checkpoint / "provenance.json").write_text(json.dumps({
        "base_model": BASE, "base_model_revision": REVISAO_BASE,
    }))
    (checkpoint / "head.safetensors").write_bytes(b"head-local")
    (checkpoint / "lora" / "adapter_model.safetensors").write_bytes(b"lora-local")
    (base / "model-00001-of-00002.safetensors").write_bytes(b"pesos-locais")
    baixar = Mock(side_effect=[str(checkpoint), str(base)])
    return checkpoint, base, baixar


def test_cache_inclui_adapter_e_base_na_revisao_da_proveniencia(
    snapshots: tuple[Path, Path, Mock], tmp_path: Path,
) -> None:
    checkpoint, base, baixar = snapshots
    destino, cache = tmp_path / "opt" / "decider-model", tmp_path / "cache"

    manifesto = preparar_modelo(destino, cache, baixar=baixar)

    assert destino.is_symlink() and destino.resolve() == checkpoint.resolve()
    assert baixar.call_args_list[0].kwargs == {
        "repo_id": MODELO, "revision": REVISAO, "cache_dir": str(cache / "hub"), "token": False,
        "allow_patterns": ARQUIVOS_CHECKPOINT,
    }
    assert baixar.call_args_list[1].kwargs["repo_id"] == BASE
    assert baixar.call_args_list[1].kwargs["revision"] == REVISAO_BASE
    assert "*.safetensors" in baixar.call_args_list[1].kwargs["allow_patterns"]
    assert baixar.call_args_list[1].kwargs["token"] is False
    assert manifesto["base_snapshot"] == str(base.resolve())
    assert json.loads((destino.parent / "decider-manifest.json").read_text()) == manifesto


def test_revisao_explicita_da_base_tambem_e_respeitada(
    snapshots: tuple[Path, Path, Mock], tmp_path: Path,
) -> None:
    checkpoint, _, baixar = snapshots
    (checkpoint / "strands_decider_config.json").write_text(json.dumps({
        "base_model": BASE, "base_revision": REVISAO_BASE, "use_lora": True,
    }))
    (checkpoint / "provenance.json").unlink()

    preparar_modelo(tmp_path / "destino", tmp_path / "cache", baixar=baixar)

    assert baixar.call_args_list[1].kwargs["revision"] == REVISAO_BASE


@pytest.mark.parametrize("alteracao", ["base", "revisao", "head", "lora", "pesos"])
def test_checkpoint_incompleto_ou_diferente_nao_produz_imagem_aparentemente_pronta(
    snapshots: tuple[Path, Path, Mock], tmp_path: Path, alteracao: str,
) -> None:
    checkpoint, base, baixar = snapshots
    if alteracao == "base":
        (checkpoint / "strands_decider_config.json").write_text('{"base_model":"outra/base"}')
    elif alteracao == "revisao":
        (checkpoint / "provenance.json").write_text(json.dumps({"base_model": BASE, "base_model_revision": "main"}))
    elif alteracao == "head":
        (checkpoint / "head.safetensors").unlink()
    elif alteracao == "lora":
        (checkpoint / "lora" / "adapter_model.safetensors").unlink()
    else:
        (base / "model-00001-of-00002.safetensors").unlink()

    destino = tmp_path / "destino"
    with pytest.raises(ValueError):
        preparar_modelo(destino, tmp_path / "cache", baixar=baixar)
    assert not destino.exists()


def test_build_nao_sobrescreve_diretorio_existente(
    snapshots: tuple[Path, Path, Mock], tmp_path: Path,
) -> None:
    _, _, baixar = snapshots
    destino = tmp_path / "destino"
    destino.mkdir()
    arquivo = destino / "preservar.txt"
    arquivo.write_text("preservar")

    with pytest.raises(ValueError, match="destino já existe"):
        preparar_modelo(destino, tmp_path / "cache", baixar=baixar)

    assert arquivo.read_text() == "preservar"


def test_revisao_mutavel_e_recusada_antes_de_baixar(tmp_path: Path) -> None:
    baixar = Mock()
    with pytest.raises(ValueError, match="SHA completo"):
        preparar_modelo(tmp_path / "destino", tmp_path / "cache", revisao="main", baixar=baixar)
    baixar.assert_not_called()
