# Interventions do Cass (passo 8)

`cass/guardrails.py` implementa `PortaoDeFerramentas` e `LimpaDados`. O agente
recebe ambos em `Agent(interventions=[portao, limpa_dados])`, nessa ordem. Cada
pergunta cria instâncias novas, tanto no endpoint JSON quanto no SSE.

## Limite de consultas

O portão aceita somente as oito ferramentas de dados do apêndice B:
`listar_hotspots`, `consultar_hotspot`, `explicar_classe`, `hipotese`, `exposicao`,
`contexto_enso`, `previsao_tempo` e `rascunhar_alerta`. Ferramentas sem dados de
stats continuam fora de `FERRAMENTAS_ATIVAS`, como antes.

Cada tentativa consome uma das seis posições, mesmo se falhar ou for recusada.
A sétima recebe `Deny`. A consulta antecipada de hotspot usa o mesmo contador:
com `hotspot_id`, restam cinco consultas para o modelo. Consultas internas que
compõem `rascunhar_alerta` fazem parte dessa chamada, sem contagem duplicada.
Chamadas negadas são identificadas pelo `toolUseId` e ficam fora da lista pública
`tools` e dos dados usados para montar a resposta.

As skills locais e a ferramenta que valida `RespostaDoModelo` são infraestrutura
existente do agente. Elas continuam disponíveis após esgotar as seis consultas e
não são truncadas. A exceção para skills compara a identidade dos objetos do
plugin criado para o pedido. A saída estruturada exige `StructuredOutputTool`
com o modelo `RespostaDoModelo`. Usar apenas esses nomes não libera outra
ferramenta.

## Limpeza dos resultados

`LimpaDados.after_tool_call` devolve `Transform(apply=...)`. A transformação
decodifica os blocos JSON antes de limitar cada texto livre a 200 caracteres,
inclusive em objetos e listas aninhados. Preserva os números, booleanos, nulos,
identificadores, enums, datas, caminhos, URLs e a proveniência (`fonte`/`fontes`).
Texto simples ou JSON inválido é tratado como texto livre, limitado e colocado
num objeto JSON com `valor`.

O resultado ganha `"_tipo": "dado"`. Objetos mantêm seus campos na raiz, para
preservar a extração de fontes, hipótese e aviso de dados de exemplo. O envelope
do Strands mantém `toolUseId`, `status` e os demais metadados. A transformação
cria cópias e não altera o cache nem o resultado original da ferramenta.

Como a consulta antecipada roda fora do Strands, ela passa pelos mesmos helpers
de autorização e limpeza antes de os dados entrarem na mensagem do modelo.

## APIs confirmadas

Imports e assinaturas foram conferidos pelo MCP `strands-agents`, com o SDK
1.65.0 instalado: `InterventionHandler`, `Deny(reason=...)`, `Proceed()` e
`Transform(apply=..., reason=...)`. `before_tool_call` aceita `Deny` e
`after_tool_call` aceita `Proceed` ou `Transform`; o SDK aplica a transformação
ao evento. Hooks continuam apenas observando.

Referências oficiais: [guia de interventions](https://strandsagents.com/docs/user-guide/sdk/agents/interventions/index.md),
[ações](https://strandsagents.com/docs/api/python/strands.interventions.actions/index.md),
[handlers](https://strandsagents.com/docs/api/python/strands.interventions.handler/index.md),
[Agent](https://strandsagents.com/docs/api/python/strands.agent.agent/index.md),
[Plugin.tools](https://strandsagents.com/docs/api/python/strands.plugins.plugin/index.md) e
[StructuredOutputTool](https://strandsagents.com/docs/api/python/strands.tools.structured_output.structured_output_tool/index.md).
