"""Testes das skills do Cass (apêndice D) e do plugin AgentSkills, sem chamar o Gemini."""

import re

import pytest

from cass.agente import PASTA_DAS_SKILLS, carregar_skills, criar_agente
from cass.config import Configuracao, carregar_config

NOMES = ["alerta-whatsapp", "contexto-el-nino", "explicar-radar", "medido-vs-hipotese"]
# Nada que leia arquivo, rode comando ou acesse a rede fora das URLs das ferramentas.
PROIBIDAS = {"file_read", "file_write", "file_editor", "editor", "shell", "python_repl", "http_request"}


@pytest.fixture
def config() -> Configuracao:
    return carregar_config({"GOOGLE_CLOUD_PROJECT": "projeto-teste"})


def instrucoes(nome: str) -> str:
    return next(s for s in carregar_skills() if s.name == nome).instructions


def normalizado(nome: str) -> str:
    """Instruções em minúsculas e sem quebras de linha, para procurar trechos."""
    return " ".join(instrucoes(nome).lower().split())


# ---------- carregamento ----------


def test_carrega_as_quatro_skills_com_nome_e_descricao() -> None:
    skills = carregar_skills()

    assert sorted(s.name for s in skills) == NOMES
    for skill in skills:
        assert skill.description.strip()
        assert skill.instructions.strip()


def test_cada_skill_mora_numa_pasta_com_o_mesmo_nome() -> None:
    pastas = sorted(p.name for p in PASTA_DAS_SKILLS.iterdir() if (p / "SKILL.md").is_file())

    assert pastas == NOMES


def test_descricoes_sao_curtas() -> None:
    # A descrição vai no prompt de sistema de toda pergunta.
    for skill in carregar_skills():
        assert len(skill.description) <= 220, skill.name


def test_skills_ficam_em_memoria_sem_caminho() -> None:
    # Sem path, o plugin não põe <location> no prompt nem lista ou lê arquivos depois.
    assert all(skill.path is None for skill in carregar_skills())


# ---------- plugin no agente ----------


def test_agente_ganha_a_ferramenta_skills_e_nenhuma_de_arquivo(config: Configuracao) -> None:
    nomes = set(criar_agente(config).tool_names)

    assert "skills" in nomes
    assert not nomes & PROIBIDAS


@pytest.mark.parametrize("nome", NOMES)
def test_ativar_a_skill_devolve_as_instrucoes(config: Configuracao, nome: str) -> None:
    agente = criar_agente(config)

    resultado = agente.tool.skills(skill_name=nome, record_direct_tool_call=False)

    assert resultado["status"] == "success"
    texto = resultado["content"][0]["text"]
    assert texto.startswith(instrucoes(nome))
    assert "Location:" not in texto
    assert "Available resources" not in texto


def test_skill_desconhecida_nao_le_nada(config: Configuracao) -> None:
    agente = criar_agente(config)

    texto = agente.tool.skills(skill_name="../.env", record_direct_tool_call=False)["content"][0]["text"]

    assert "not found" in texto
    assert "GOOGLE_CLOUD_PROJECT" not in texto


# ---------- conteúdo que as regras exigem ----------


def test_medido_vs_hipotese_sem_dados_nao_oferece_hipotese() -> None:
    texto = normalizado("medido-vs-hipotese")

    assert "sem esses dados, não ofereça hipótese nenhuma" in texto
    assert "diga que a hipótese ainda não foi calculada" in texto


@pytest.mark.parametrize(
    ("nome", "trechos"),
    [
        ("alerta-whatsapp", ["rascunhar_alerta", "nada é enviado", "sem alarmismo"]),
        ("explicar-radar", ["banda L", "HH", "HV", "coerência", "nuvem", "fumaça"]),
        ("contexto-el-nino", ["tendência", "não garantia", "NOAA CPC (contexto regional)"]),
        ("medido-vs-hipotese", ["número, unidade, data e fonte", "fraca, moderada ou forte"]),
    ],
)
def test_skill_cobre_o_apendice_d(nome: str, trechos: list[str]) -> None:
    texto = normalizado(nome)

    for trecho in trechos:
        assert trecho.lower() in texto, trecho


@pytest.mark.parametrize("nome", NOMES)
def test_nenhuma_skill_afirma_que_o_el_nino_causou(nome: str) -> None:
    # As skills podem dizer "nunca afirme que o El Niño causou", mas não afirmar.
    texto = normalizado(nome)

    for achado in re.finditer(r"el niño causou|causad[oa] pelo el niño", texto):
        antes = texto[max(0, achado.start() - 40) : achado.start()]
        assert "nunca" in antes or "não" in antes, texto[achado.start() - 40 : achado.end() + 20]
