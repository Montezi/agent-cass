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
