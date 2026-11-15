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

## Decider e camada de entrada (passo 9)

`decider_sim(estado, pergunta, padrao)` só consulta o Decider quando
`DECIDER_ATIVO` está ligado. Faz POST em `DECIDER_URL`, com timeout de 1,5 s:

```json
{"state":"conversa","questions":{"q":{"type":"noul","instructions":"pergunta"}}}
```

Lê `answers.q.noul`, exigindo número finito entre zero e um. Serviço desligado,
falha HTTP/rede, JSON inválido ou resposta fora do formato devolvem `padrao`.
Chamadores assíncronos executam esse cliente em thread separada.

A camada 1 aplica o limite de 500 caracteres do `api-contract.md` do frontend
(acima disso, 422 na validação do pedido), regras fixas para
pedidos explícitos de troca de instruções e alguns temas claramente alheios,
e as duas perguntas do apêndice C: escopo abaixo de 0,3 (padrão 1,0) ou tentativa
de injeção acima de 0,7 (padrão 0,0) devolvem `fora_do_escopo` sem executar o
agente. As regras fixas não substituem um classificador geral; os demais pedidos
seguem para o agente quando o Decider está desligado ou indisponível.

## Steering e ledger

`SteeringCass` é criado por pergunta e registrado em `Agent(plugins=[...])`.
Os imports confirmados pelo MCP são de `strands.vended_plugins.steering`:
`SteeringHandler`, `Proceed`, `Guide` e `LedgerProvider`. As ações de steering
exigem `reason` e são diferentes das ações homônimas de interventions.

- `async steer_before_tool(self, *, agent, tool_use, **kwargs)`: se há argumento
  `id` e nenhum hotspot aberto, consulta o Decider com padrão 0,0. Abaixo de 0,5,
  `Guide` orienta a perguntar e oferecer opções, sem adivinhar o hotspot.
- `async steer_after_model(self, *, agent, message, stop_reason, **kwargs)`:
  examina a candidata final. Probabilidade acima de 0,5, ou uma afirmação causal
  explícita reconhecida pelas regras PT/EN, solicita reescrita como hipótese.
  No máximo uma orientação desse tipo por pergunta.

O fluxo atual encerra pela ferramenta `RespostaDoModelo`, com
`stop_reason="tool_use"`; o handler examina seus argumentos também, além de
respostas com `end_turn`. O `Guide` após o modelo descarta a candidata antes de
executar a ferramenta de saída. Uma causalidade que persistir passa pela
verificação final e pelo limite de tentativas da camada 4.

O ledger é instalado explicitamente com `LedgerProvider()` e acessado em
`self.steering_context.data.get("ledger")`. Possui `session_start`, `tool_calls`,
`conversation_history` e `session_metadata`. Cada chamada registra `timestamp`,
`tool_use_id`, `tool_name`, `tool_args` e `status="pending"`; ao concluir, recebe
`completion_timestamp`, `status` (`success`/`error`), `result` e `error`.
`pending` não significa execução concluída. Um resultado `success` pode conter
`erro` ou `disponivel: false` do Cass. A consulta antecipada não passa por esse
ledger; continua incluída explicitamente na cota, no progresso e nas evidências.
Chamadas canceladas pelo steering também ficam fora de `tools` e das evidências.

Referências confirmadas:
[SteeringHandler](https://strandsagents.com/docs/api/python/strands.vended_plugins.steering.core.handler/index.md),
[ações](https://strandsagents.com/docs/api/python/strands.vended_plugins.steering.core.action/index.md) e
[ledger](https://strandsagents.com/docs/api/python/strands.vended_plugins.steering.context_providers.ledger_provider/index.md).

## Verificação da saída e repetição

Os dois endpoints fornecem o mesmo callback de verificação ao agente.
`cass/verificacao.py` confere cada número em `texto` e `medido` com os resultados
das ferramentas, incluindo o hotspot antecipado. Trata formatos PT/EN, datas e
confiança em porcentagem. IDs, URLs, nomes de fontes, coordenadas, bbox, flags,
erros e valores nulos não autorizam uma medição. A verificação compara valores;
não constitui uma validação semântica completa de toda afirmação do modelo.

Na primeira falha, solicita uma correção no mesmo Agent. Na segunda falha,
devolve texto fixo no idioma do pedido, `confianca="baixa"`, sem números não
confirmados, medido ou hipótese, preservando fontes e aviso de dados de exemplo.
O formato público continua com `paragraphs`, `tools`, `sources` e `note`.

Preparação, decisões de entrada, steering e repetição compartilham o prazo total
de 20 s. A correção não zera o portão nem repete automaticamente o prefetch.
O hook finaliza uma única linha JSON por pergunta, após a verificação, com o tipo
efetivamente devolvido e o tempo total.

## Proteções HTTP

CORS permite somente `APP_ORIGIN`, com GET/POST, cabeçalhos Content-Type/Accept,
sem credenciais ou origem curinga. O limite inicial é de **10 perguntas por
minuto por IP**, compartilhado entre JSON e SSE. Ao exceder, responde HTTP 429
com corpo no formato público e `Retry-After`; no SSE isso ocorre antes da abertura
do stream. `/health` e preflight OPTIONS não consomem essa cota.

O contador usa memória do processo, relógio monotônico e até 10.000 IPs ativos.
Não é uma cota global entre instâncias/reinícios do Cloud Run.

O IP é o primeiro endereço do `X-Forwarded-For` (`cass.limites.ip_do_cliente`).
No Cloud Run, `request.client.host` é o proxy do Google, e o endereço de quem
chamou vem no início desse cabeçalho. Sem o cabeçalho, ou se o primeiro item não
for um IP, vale o `request.client.host`.

**O cabeçalho pode ser falsificado.** Quem chama pode mandar o próprio
`X-Forwarded-For`, e o primeiro endereço passa a ser o que ele escolheu; trocando
o valor a cada pedido, ganha cota nova. Este limite só freia o uso casual. O teto
de custo real é o `--max-instances` do Cloud Run (ver `docs/deploy.md`): o Cloud
Run não abre instâncias além dele, então os pedidos simultâneos ficam limitados a
`--max-instances` × `--concurrency` (2 × 80 na variante leve, com a concorrência
padrão).

O `hotspot_id` precisa seguir o formato de id do contrato de dados e ser um dos ids
do `hotspots.geojson` (api-contract.md do frontend). Fora do formato, a validação
do pedido responde 422; um id que não está na lista também recebe 422, no mesmo
formato de erro, com os ids válidos em `ctx.validos`. A conferência vem depois do
limite por IP e, no SSE, antes de abrir o stream; usa o mesmo cache de 5 minutos
das ferramentas. Se o `hotspots.geojson` não puder ser lido, o pedido segue sem
`hotspot_id`: um id sem conferência não chega ao steering (que trataria o hotspot
como aberto) nem ao prompt.

## Bateria de avaliação

`tests/test_bateria.py` contém os nove casos do apêndice E, marcados `gemini`.
Usa dados locais com `metadata.sample=true`, previsão simulada indisponível e
`DECIDER_ATIVO=false`. O fixture não tem medições de radar nem hipótese calculada;
os testes não exigem números ou evidência que esses dados não fornecem.

```bash
.venv/bin/pytest -q             # testes locais, sem chamadas ao Gemini
.venv/bin/pytest -q -m gemini   # ADC e configuração Vertex AI; chama o Gemini real
```
