"""Regras duras do Cass com eventos reais do Strands, sem rede ou Gemini."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from strands import AgentSkills, Skill
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.interventions import Deny, Proceed, Transform
from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

from cass.guardrails import (
    MAX_CHAMADAS,
    MAX_TEXTO_LIVRE,
    PERMITIDAS,
    LimpaDados,
    PortaoDeFerramentas,
)
from cass.modelos import RespostaDoModelo


def antes(nome: str = "consultar_hotspot", ferramenta: Any = None) -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=SimpleNamespace(messages=[]),
        selected_tool=ferramenta,
        tool_use={"toolUseId": "consulta-1", "name": nome, "input": {"id": "guaiba-rs"}},
        invocation_state={"pedido": "teste"},
    )


def depois(
    conteudo: list[dict[str, Any]],
    nome: str = "consultar_hotspot",
    ferramenta: Any = None,
    status: str = "success",
    excecao: Exception | None = None,
) -> AfterToolCallEvent:
    return AfterToolCallEvent(
        agent=SimpleNamespace(messages=[]),
        selected_tool=ferramenta,
        tool_use={"toolUseId": "consulta-1", "name": nome, "input": {"id": "guaiba-rs"}},
        invocation_state={"pedido": "teste"},
        result={"toolUseId": "consulta-1", "status": status, "content": conteudo},
        duration=0.25,
        exception=excecao,
    )


def texto_json(valor: Any) -> list[dict[str, Any]]:
    return [{"text": json.dumps(valor, ensure_ascii=False)}]


def aplicar_limpeza(evento: AfterToolCallEvent, limpa: LimpaDados | None = None) -> None:
    acao = (limpa if limpa is not None else LimpaDados()).after_tool_call(evento)
    assert isinstance(acao, Transform)
    acao.apply(evento)


def ferramenta_skills() -> Any:
    skill = Skill.from_content(
        "---\nname: teste-local\ndescription: Conhecimento local de teste.\n---\n"
        "Siga as regras da skill sem acesso a arquivos."
    )
    return AgentSkills(skills=[skill]).tools[0]


def test_sexta_consulta_passa_e_setima_e_as_seguintes_sao_negadas() -> None:
    portao = PortaoDeFerramentas()

    decisoes = [portao.before_tool_call(antes()) for _ in range(9)]

    assert MAX_CHAMADAS == 6
    assert all(isinstance(d, Proceed) for d in decisoes[:6])
    assert all(isinstance(d, Deny) and d.reason for d in decisoes[6:])
    assert portao.chamadas == 9


def test_lista_tem_somente_as_oito_ferramentas_do_cass() -> None:
    assert isinstance(PERMITIDAS, frozenset)
    assert PERMITIDAS == {
        "listar_hotspots", "consultar_hotspot", "explicar_classe", "hipotese",
        "exposicao", "contexto_enso", "previsao_tempo", "rascunhar_alerta",
    }
    for nome in PERMITIDAS:
        assert isinstance(PortaoDeFerramentas().before_tool_call(antes(nome)), Proceed)


@pytest.mark.parametrize(
    "nome", ["shell", "file_read", "web_search", "", "skills", "RespostaDoModelo"]
)
def test_nome_nao_permitido_e_negado_e_conta_como_tentativa(nome: str) -> None:
    portao = PortaoDeFerramentas()

    assert isinstance(portao.before_tool_call(antes(nome)), Deny)
    assert portao.chamadas == 1


def test_autorizacao_da_consulta_antecipada_consume_o_mesmo_limite() -> None:
    portao = PortaoDeFerramentas()

    assert isinstance(portao.autorizar("consultar_hotspot"), Proceed)
    for _ in range(5):
        assert isinstance(portao.before_tool_call(antes("listar_hotspots")), Proceed)
    assert isinstance(portao.before_tool_call(antes()), Deny)


def test_contador_e_independente_por_pedido_e_respeita_limite_configurado() -> None:
    primeiro = PortaoDeFerramentas(max_chamadas=1)
    segundo = PortaoDeFerramentas(max_chamadas=1)

    assert isinstance(primeiro.before_tool_call(antes()), Proceed)
    assert isinstance(primeiro.before_tool_call(antes()), Deny)
    assert segundo.chamadas == 0
    assert isinstance(segundo.before_tool_call(antes()), Proceed)


def test_chamadas_concorrentes_nao_ultrapassam_seis_autorizacoes() -> None:
    portao = PortaoDeFerramentas()

    async def executar() -> list:
        async def consultar() -> Any:
            await asyncio.sleep(0)
            return portao.before_tool_call(antes())

        return await asyncio.gather(*(consultar() for _ in range(20)))

    decisoes = asyncio.run(executar())

    assert sum(isinstance(d, Proceed) for d in decisoes) == 6
    assert sum(isinstance(d, Deny) for d in decisoes) == 14
    assert portao.chamadas == 20


def test_portao_nao_altera_os_argumentos_estado_ou_cancelamento_do_evento() -> None:
    portao = PortaoDeFerramentas()
    evento = antes("shell")
    original = deepcopy((evento.tool_use, evento.invocation_state, evento.agent.messages))

    assert isinstance(portao.before_tool_call(evento), Deny)

    assert (evento.tool_use, evento.invocation_state, evento.agent.messages) == original
    assert evento.cancel_tool is False
    assert portao.negadas == {evento.tool_use["toolUseId"]}


def test_saida_estruturada_real_passa_mesmo_apos_o_limite_e_nao_sofre_limpeza() -> None:
    ferramenta = StructuredOutputTool(RespostaDoModelo)
    portao = PortaoDeFerramentas(max_chamadas=1)
    assert isinstance(portao.autorizar("consultar_hotspot"), Proceed)
    limpa = LimpaDados()
    evento = depois(
        [{"text": "Confirmação interna " + "x" * 300}], ferramenta.tool_name, ferramenta
    )
    original = deepcopy(evento.result)

    assert isinstance(portao.before_tool_call(antes(ferramenta.tool_name, ferramenta)), Proceed)
    assert portao.chamadas == 1
    assert isinstance(limpa.after_tool_call(evento), Proceed)
    assert evento.result == original


def test_so_o_objeto_de_skills_registrado_ganha_excecao_de_infraestrutura() -> None:
    confiavel = ferramenta_skills()
    substituta = ferramenta_skills()
    portao = PortaoDeFerramentas(max_chamadas=1)
    assert isinstance(portao.autorizar("consultar_hotspot"), Proceed)
    limpa = LimpaDados()
    portao.registrar_ferramentas_internas([confiavel])
    limpa.registrar_ferramentas_internas([confiavel])
    evento = depois([{"text": "Instruções locais " + "x" * 500}], "skills", confiavel)
    original = deepcopy(evento.result)

    assert isinstance(portao.before_tool_call(antes("skills", confiavel)), Proceed)
    assert portao.chamadas == 1
    assert isinstance(limpa.after_tool_call(evento), Proceed)
    assert evento.result == original
    assert isinstance(portao.before_tool_call(antes("skills", substituta)), Deny)
    substituido = depois(texto_json({"texto": "x" * 300}), "skills", substituta)
    assert isinstance(limpa.after_tool_call(substituido), Transform)
    assert isinstance(portao.before_tool_call(antes("shell", confiavel)), Deny)


def test_tipo_ou_nome_de_saida_falsificado_nao_autoriza_ferramenta() -> None:
    falsa = SimpleNamespace(
        tool_name="RespostaDoModelo", tool_type="structured_output",
        structured_output_model=RespostaDoModelo,
    )
    portao = PortaoDeFerramentas()

    assert isinstance(portao.before_tool_call(antes("RespostaDoModelo", falsa)), Deny)
    verdadeira = StructuredOutputTool(RespostaDoModelo)
    assert isinstance(portao.before_tool_call(antes("shell", verdadeira)), Deny)

    class OutroModelo(BaseModel):
        texto: str

    outra = StructuredOutputTool(OutroModelo)
    assert isinstance(portao.before_tool_call(antes(outra.tool_name, outra)), Deny)


@pytest.mark.parametrize("tamanho", [199, 200, 201, 1000])
def test_limpeza_respeita_200_caracteres_e_preserva_unicode(tamanho: int) -> None:
    valor = {"texto": "á" * tamanho, "aninhado": [{"descricao": "🌧" * tamanho}]}
    original = deepcopy(valor)

    limpo = LimpaDados().limpar_dados(valor)

    assert MAX_TEXTO_LIVRE == 200
    assert limpo["texto"] == "á" * min(tamanho, 200)
    assert limpo["aninhado"][0]["descricao"] == "🌧" * min(tamanho, 200)
    assert limpo["_tipo"] == "dado"
    assert valor == original and limpo is not valor


def test_limpeza_preserva_medicoes_campos_tecnicos_fontes_e_chaves() -> None:
    longo = "preservar-" * 30
    chave = "chave-extensa-" * 30
    fonte = {"nome": longo, "data": "2026-11-15", "url": "https://fonte.test/" + longo}
    valor = {
        "hotspot": {
            "id": longo,
            "name": {"pt": "x" * 300, "en": "y" * 300},
            "short_name": {"pt": "z" * 300, "en": "w" * 300},
            "dominant_class": "flood",
            "affected_area_km2": 412.5,
            "confidence": 0.87,
            "last_pass": "2026-11-12",
            "layers": {"flood": "layers/" + longo + "/flood.png"},
        },
        "hotspot_id": longo,
        "validos": [longo],
        "fonte": fonte,
        "fontes": [fonte],
        "source": "meteomatics",
        "updated_at": "2026-11-15T12:30:00Z",
        "atualizada_em": "2026-11-15T12:30:00Z",
        "dias": [{"date": "2026-11-16", "precipitation_mm": 0}],
        "hipotese": {"forca": "forte", "strength": "strong", "texto": "t" * 300},
        "dados_de_exemplo": True,
        "disponivel": False,
        "nulo": None,
        "enviado": False,
        chave: 12.5,
    }

    limpo = LimpaDados().limpar_dados(valor)

    assert limpo["hotspot"]["name"] == {"pt": "x" * 200, "en": "y" * 200}
    assert limpo["hotspot"]["short_name"] == {"pt": "z" * 200, "en": "w" * 200}
    assert limpo["hipotese"]["texto"] == "t" * 200
    esperado = deepcopy(valor)
    esperado["hotspot"]["name"] = limpo["hotspot"]["name"]
    esperado["hotspot"]["short_name"] = limpo["hotspot"]["short_name"]
    esperado["hipotese"]["texto"] = limpo["hipotese"]["texto"]
    esperado["_tipo"] = "dado"
    assert limpo == esperado


def test_transform_preserva_envelope_estado_e_resultado_original_com_json_valido() -> None:
    dados = {"erro": "x" * 300, "rascunho": 'a"\n\\' * 90, "fontes": [{"nome": "NISAR"}]}
    evento = depois(texto_json(dados), status="error", excecao=RuntimeError("falha original"))
    evento.retry = True
    resultado_original = evento.result
    original = deepcopy(resultado_original)
    estado = deepcopy((evento.tool_use, evento.invocation_state))

    aplicar_limpeza(evento)

    assert evento.result is not resultado_original
    assert resultado_original == original
    assert evento.result["toolUseId"] == "consulta-1"
    assert evento.result["status"] == "error"
    limpo = json.loads(evento.result["content"][0]["text"])
    assert limpo["erro"] == "x" * 200
    assert limpo["rascunho"] == dados["rascunho"][:200]
    assert limpo["fontes"] == dados["fontes"]
    assert limpo["_tipo"] == "dado"
    assert (evento.tool_use, evento.invocation_state) == estado
    assert str(evento.exception) == "falha original"
    assert evento.retry is True


@pytest.mark.parametrize("valor", [["x" * 250, 1, None], "x" * 250, 42, True, None])
def test_valores_sem_objeto_raiz_recebem_marcacao_sem_perder_tipo(valor: Any) -> None:
    evento = depois(texto_json(valor))

    aplicar_limpeza(evento)

    limpo = json.loads(evento.result["content"][0]["text"])
    if isinstance(valor, list):
        esperado = ["x" * 200, 1, None]
    else:
        esperado = valor[:200] if isinstance(valor, str) else valor
    assert limpo == {"_tipo": "dado", "valor": esperado}


@pytest.mark.parametrize("texto", ["Texto livre " * 30, '{"json quebrado": ' + "x" * 300])
def test_texto_livre_ou_json_invalido_viram_dado_sem_derrubar_a_chamada(texto: str) -> None:
    evento = depois([{"text": texto}])

    aplicar_limpeza(evento)

    limpo = json.loads(evento.result["content"][0]["text"])
    assert limpo == {"_tipo": "dado", "valor": texto[:200]}


def test_varios_blocos_texto_e_json_sao_limpos_sem_alterar_o_original() -> None:
    imagem = {"image": {"format": "png", "source": {"bytes": b"imagem-local"}}}
    conteudo = [
        {"text": json.dumps({"motivo": "x" * 201})},
        {"json": {"descricao": "y" * 201, "valor": 2.5}},
        imagem,
        {"text": "z" * 201},
    ]
    original = deepcopy(conteudo)
    evento = depois(conteudo)

    aplicar_limpeza(evento)

    assert conteudo == original
    blocos = evento.result["content"]
    assert json.loads(blocos[0]["text"]) == {"motivo": "x" * 200, "_tipo": "dado"}
    assert blocos[1]["json"] == {"descricao": "y" * 200, "valor": 2.5, "_tipo": "dado"}
    assert blocos[2] == imagem
    assert json.loads(blocos[3]["text"]) == {"valor": "z" * 200, "_tipo": "dado"}


def test_marcacao_de_dado_nao_pode_ser_sobrescrita_pelo_resultado() -> None:
    valor = {"_tipo": "instrução", "texto": "x" * 201}

    assert LimpaDados().limpar_dados(valor) == {"_tipo": "dado", "texto": "x" * 200}
