# Proposta: campos opcionais na resposta do `POST /api/agente`

Status: proposta, aguardando decisão do frontend. Contrato vigente:
[`api-contract.md`](api-contract.md) (zod `agentAnswerSchema` em
`src/features/cass/agentApi.ts`).

## Contexto

Por dentro, o Cass produz uma `RespostaCass` (apêndice A do plano de
desenvolvimento), e `cass/contrato.py` a converte para os quatro campos do
contrato: `paragraphs`, `tools`, `sources` e `note`. Parte da `RespostaCass` não
tem lugar nesses quatro campos. Hoje ela vira texto dentro de `paragraphs` ou se
perde.

## O que a `RespostaCass` tem e onde vai parar hoje

| Campo da `RespostaCass`          | Hoje, no contrato                                               | Perde o quê                                    |
| -------------------------------- | --------------------------------------------------------------- | ---------------------------------------------- |
| `texto`                          | `paragraphs` (um por bloco separado por linha em branco)        | nada                                           |
| `medido`                         | um parágrafo `"Medido: …; …."`                                  | a lista separada (para um bloco "Medido")      |
| `hipotese` (texto, evidência, força) | um parágrafo `"Hipótese (força moderada): …. Evidência: …."` | a força como valor (para um selo)              |
| `fontes` (nome, data, ferramenta) | `sources`: `"nome, data"`                                      | `ferramenta`, que `tools` já cobre             |
| `dados_de_exemplo`               | `note`: "Dados de exemplo: posições aproximadas…"               | o booleano (para o mesmo selo de `/dados`)     |
| `opcoes`                         | um parágrafo `"Opções: A, B."`                                  | os botões                                      |
| `tipo`                           | não aparece (`erro` vira HTTP 500/504)                          | saber se é resposta, pergunta de volta ou recusa |
| `confianca`                      | não aparece                                                     | tudo                                           |
| `sugestoes`                      | não aparece                                                     | tudo                                           |
| `idioma`                         | não precisa: é o `language` do pedido                           | nada                                           |

`tools` não vem da `RespostaCass`: o serviço calcula a partir das ferramentas que
foram de fato chamadas. Quando o pedido traz `hotspot_id`, `consultar_hotspot`
roda antes do modelo e entra na lista.

## Proposta

Todos os campos são **opcionais**. O `agentAnswerSchema` atual descarta chaves
desconhecidas (conferido com o zod do frontend: o parse passa e remove a chave).
Então o serviço pode começar a mandá-los antes de o app saber lê-los, sem quebrar
nada.

```ts
export const agentAnswerSchema = z.object({
  paragraphs: z.array(z.string().min(1)).min(1),
  tools: z.array(z.string()),
  sources: z.array(z.string()),
  note: z.string().nullable().optional(),

  // Propostos. Todos opcionais: sem eles, o app se comporta como hoje.
  /** answer: respondeu; clarify: o hotspot não ficou claro; out_of_scope: fora do tema. */
  kind: z.enum(['answer', 'clarify', 'out_of_scope']).optional(),
  /** Botões quando kind = "clarify" ("Lago Guaíba", "Lago Kariba"). */
  options: z.array(z.string().min(1)).optional(),
  /** Próximas perguntas, como chips. */
  suggestions: z.array(z.string().min(1)).optional(),
  /** Confiança do Cass na resposta, a partir dos dados que usou. */
  confidence: z.enum(['high', 'medium', 'low']).optional(),
  /** true quando a resposta usou dados com metadata.sample = true. */
  sample: z.boolean().optional(),
  /** Frases do que o radar mediu, com número, unidade e data. **x** marca o número. */
  measured: z.array(z.string().min(1)).optional(),
  /** Mesmo formato de `hypothesis` em stats/<id>.json (data-contract.md). */
  hypothesis: z
    .object({
      text: z.string().min(1),
      evidence: z.string().min(1),
      strength: z.enum(['weak', 'moderate', 'strong']),
    })
    .nullable()
    .optional(),
});
```

De-para dos valores: `tipo` resposta / pergunta_de_volta / fora_do_escopo vira
`kind` answer / clarify / out_of_scope. `confianca` alta / media / baixa vira
`confidence` high / medium / low. `forca` fraca / moderada / forte vira `strength`
weak / moderate / strong.

## Ordem de adoção

1. **Campos que não repetem conteúdo:** `kind`, `options`, `suggestions`,
   `confidence` e `sample`. O serviço passa a mandá-los, e o app adota quando
   quiser. `options` continua também no parágrafo "Opções: …" até o app mostrar os
   botões.
2. **`measured` e `hypothesis`:** esses dois hoje já estão dentro de `paragraphs`.
   Para não aparecerem duas vezes:
   1. o serviço manda os campos e mantém os parágrafos (nada muda na tela);
   2. o app passa a mostrar os campos;
   3. o serviço tira os parágrafos "Medido:" e "Hipótese:".

   Entre os passos 2 e 3, a resposta aparece repetida, então convém fazer os dois
   em sequência.

## Decisão pedida ao frontend

- Quais campos adotar, e se os nomes e valores acima servem.
- Se `options` em `kind = "clarify"` deve mandar só o texto do botão ou também o
  `hotspot_id` de cada opção, para o app abrir a gaveta certa. Nesse caso,
  `options` passaria a ser `z.array(z.object({ label, hotspot_id }))`.

## Proposta: `POST /api/agente/stream` em SSE

Status: implementada no serviço, proposta para adoção pelo frontend. O contrato
vigente em `api-contract.md` não define streaming. O `POST /api/agente` continua
com seu JSON, campos e tratamento de status atuais. Esta rota é adicional.

O pedido é o mesmo do endpoint JSON, com `Content-Type: application/json`:

```json
{ "question": "O que mudou no Guaíba?", "hotspot_id": "guaiba-rs", "language": "pt" }
```

Pedidos inválidos recebem HTTP `422` JSON antes de abrir o stream. Pedidos válidos
recebem HTTP `200`, `Content-Type: text/event-stream; charset=utf-8`,
`Cache-Control: no-cache` e `X-Accel-Buffering: no`.

Cada evento usa `event: <nome>` e `data: <JSON>` em UTF-8, seguido de uma linha
em branco. O frontend pode traduzir os rótulos de progresso conforme `language`:

| Evento | `data` | Uso pelo frontend |
| --- | --- | --- |
| `pensando` | `{}` | Mostrar “Cass está pensando”. Ocorre no início e ao retomar após as ferramentas. |
| `ferramenta` | `{"nome":"consultar_hotspot"}` | Mostrar “Consultando…” e o nome da ferramenta. Pode repetir. |
| `resposta` | O objeto com `paragraphs`, `tools`, `sources`, `note` | Validar com o mesmo esquema da resposta JSON e mostrar a resposta. Terminal. |
| `erro` | `{"status":500,"response":{"paragraphs":["…"],"tools":[],"sources":[],"note":null}}` | Mostrar o erro localizado em `response`, com opção de tentar de novo. Terminal. |

O stream termina depois de um único evento terminal, `resposta` ou `erro`.
No evento `resposta`, o objeto é diretamente o resultado da mesma conversão de
`RespostaCass` usada no `/api/agente`; nenhum campo proposto acima é acrescentado.
Exemplo simplificado, sem dados de exemplo:

```text
event: pensando
data: {}

event: ferramenta
data: {"nome":"consultar_hotspot"}

event: pensando
data: {}

event: resposta
data: {"paragraphs":["O radar ainda não tem medição."],"tools":["consultar_hotspot"],"sources":[],"note":null}

```

Quando há `hotspot_id`, a consulta antecipada existente de `consultar_hotspot`
também publica progresso e entra na medição de tempo. Os callbacks observam as
chamadas reais do Strands, incluindo a ferramenta `skills` quando usada; a
ferramenta interna `RespostaDoModelo`, que apenas estrutura a saída, fica de fora.
A lista pública `tools` continua seguindo a regra atual: ferramentas de dados
do Cass, sem repetição e na ordem em que foram usadas. Os eventos de progresso
não contêm argumentos, resultados, tokens do modelo ou raciocínio interno.

O limite existente de **20 segundos** inclui preparação e invocação do agente.
Como os cabeçalhos SSE já foram enviados, falhas usam HTTP `200` com o evento
`erro`: `status: 504` para o limite excedido e `status: 500` para outras falhas,
inclusive resposta inválida. `response` tem o mesmo formato e texto localizado
dos erros do endpoint JSON, sem detalhes internos da exceção.

Ao desconectar, o servidor cancela e aguarda a tarefa do agente. Uma chamada
síncrona que já esteja executando numa thread pode terminar até seu timeout de
rede, mesmo após esse cancelamento. Cada pedido tem sua própria fila e instância
do hook. Não há retomada por `Last-Event-ID` nem reconexão automática: para tentar
novamente, o frontend faz outro POST. Encerrar a conexão sem evento terminal deve
ser tratado como erro de rede. O frontend mantém o timeout de 20 s e pode usar
`AbortController` ao fechar a conversa.

Para conferir localmente, com o serviço na porta 8080:

```bash
curl -N -X POST http://localhost:8080/api/agente/stream \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  --data '{"question":"O que mudou no Guaíba?","hotspot_id":"guaiba-rs","language":"pt"}'
```

O navegador deve usar `fetch`, pois `EventSource` envia GET. Exemplo de leitura
incremental; `aoEvento` entrega o nome e o JSON à interface:

```ts
async function perguntarEmStream(
  pedido: { question: string; hotspot_id?: string; language: 'pt' | 'en' },
  aoEvento: (nome: string, dados: unknown) => void,
  signal?: AbortSignal,
) {
  const response = await fetch('/api/agente/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(pedido),
    signal,
  });
  if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let terminal = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let fim: number;
      while ((fim = buffer.indexOf('\n\n')) >= 0) {
        const bloco = buffer.slice(0, fim);
        buffer = buffer.slice(fim + 2);
        const linhas = bloco.split('\n');
        const nome = linhas.find(l => l.startsWith('event: '))?.slice(7);
        const json = linhas.filter(l => l.startsWith('data: ')).map(l => l.slice(6)).join('\n');
        if (!nome || !json) continue;
        aoEvento(nome, JSON.parse(json));
        terminal ||= nome === 'resposta' || nome === 'erro';
      }
      if (done) break;
    }
    if (!terminal) throw new Error('Stream encerrado sem resposta');
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}
```

O proxy/rewrite de publicação também precisa encaminhar `/api/agente/stream`
ao serviço e permitir entrega incremental; esta implementação não altera a
configuração do Firebase Hosting. A compatibilidade SSE desse caminho deve ser
conferida na integração do frontend.

### Registro de execução

`ProgressoERegistro` observa sem alterar argumentos, resultados ou decisões do
agente. Ao terminar cada pedido, imprime uma única linha JSON no stdout:

```json
{"ferramentas":[{"nome":"consultar_hotspot","tempo_s":0.02}],"tempo_total_s":3.4,"tipo":"resposta"}
```

Os tempos são em segundos, medidos com relógio monotônico. Cada chamada tem sua
entrada, inclusive quando a mesma ferramenta é chamada novamente ou em paralelo.
`tipo` é o tipo da resposta interna (`resposta`, `pergunta_de_volta`,
`fora_do_escopo`, `erro`); falha ou cancelamento sem saída estruturada válida usa
`erro`. O registro não inclui pergunta, argumentos, resultados nem texto da
resposta. O endpoint JSON também recebe esse registro, sem mudar seu corpo HTTP.

APIs conferidas com o MCP `strands-agents`: `HookProvider.register_hooks`,
`HookRegistry.add_callback`, `BeforeInvocationEvent`, `BeforeToolCallEvent`,
`AfterToolCallEvent` (`duration`, em segundos) e `AfterInvocationEvent` (`result`,
opcional). Referências oficiais:
[registro](https://strandsagents.com/docs/api/python/strands.hooks.registry/index.md),
[eventos](https://strandsagents.com/docs/api/python/strands.hooks.events/index.md) e
[hooks](https://strandsagents.com/docs/user-guide/sdk/agents/hooks/index.md).
