"""Prepara o checkpoint e a base no build, para servir sem downloads em runtime."""

import argparse
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

MODELO = "StrandsAgents/strands-decider-2B-qwen3.5-v1-2610"
REVISAO = "216d1ccb46a9e9b036eb9187b248db98480f77d4"
BASE = "Qwen/Qwen3.5-2B-Base"
REVISAO_BASE = "b1485b2fa6dfa1287294f269f5fb618e03d52d7c"
ARQUIVOS_CHECKPOINT = [
    "strands_decider_config.json", "provenance.json", "head.safetensors", "lora/*",
    "tokenizer*", "chat_template.jinja", "special_tokens_map.json", "LICENSE*", "MANIFEST.sha256",
]
ARQUIVOS_BASE = [
    "*.json", "*.safetensors", "*.model", "tokenizer*", "merges.txt", "vocab*",
    "*.jinja", "LICENSE*",
]


def preparar_modelo(
    destino: Path,
    cache: Path,
    revisao: str = REVISAO,
    baixar: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Baixa revisões fixas; a base requerida pelo adapter também precisa do cache.

    O checkpoint publicado contém LoRA e head, não os pesos completos da base.
    O destino é um link para seu snapshot fixo, evitando buscar `main` ao servir.
    """
    if re.fullmatch(r"[a-f0-9]{40}", revisao) is None:
        raise ValueError("A revisão do checkpoint deve ser um SHA completo do Hub.")
    if baixar is None:
        from huggingface_hub import snapshot_download
        baixar = snapshot_download

    cache_hub = cache / "hub"
    checkpoint = Path(baixar(
        repo_id=MODELO, revision=revisao, cache_dir=str(cache_hub), token=False,
        allow_patterns=ARQUIVOS_CHECKPOINT,
    )).resolve()
    config = json.loads((checkpoint / "strands_decider_config.json").read_text(encoding="utf-8"))
    if config.get("base_model") != BASE:
        raise ValueError("O checkpoint não declara a base Qwen esperada.")
    revisao_base = config.get("base_revision")
    if not revisao_base:
        proveniencia = json.loads((checkpoint / "provenance.json").read_text(encoding="utf-8"))
        if proveniencia.get("base_model") != BASE:
            raise ValueError("A proveniência não corresponde à base do checkpoint.")
        revisao_base = proveniencia.get("base_model_revision")
    if revisao_base != REVISAO_BASE:
        raise ValueError("A revisão da base difere da revisão avaliada para esta imagem.")
    if not (checkpoint / "head.safetensors").is_file():
        raise ValueError("O checkpoint está sem o head.")
    if config.get("use_lora") and not (checkpoint / "lora" / "adapter_model.safetensors").is_file():
        raise ValueError("O checkpoint está sem os pesos LoRA.")

    base = Path(baixar(
        repo_id=BASE, revision=revisao_base, cache_dir=str(cache_hub),
        allow_patterns=ARQUIVOS_BASE, token=False,
    )).resolve()
    if not any(base.glob("*.safetensors")):
        raise ValueError("Os pesos da base não foram baixados.")

    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.is_symlink():
        if destino.resolve() != checkpoint:
            raise ValueError("O destino já aponta para outro checkpoint.")
    elif destino.exists():
        raise ValueError("O destino já existe e não é um link para o checkpoint.")
    else:
        destino.symlink_to(checkpoint, target_is_directory=True)
    manifesto = {
        "model": MODELO, "revision": revisao, "checkpoint": str(checkpoint),
        "base_model": BASE, "base_revision": revisao_base, "base_snapshot": str(base),
    }
    (destino.parent / "decider-manifest.json").write_text(
        json.dumps(manifesto, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return manifesto


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destino", type=Path, default=Path("/opt/decider-model"))
    parser.add_argument("--cache", type=Path, default=Path("/opt/decider-cache"))
    parser.add_argument("--revision", default=REVISAO)
    argumentos = parser.parse_args()
    manifesto = preparar_modelo(argumentos.destino, argumentos.cache, argumentos.revision)
    print(json.dumps(manifesto, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
