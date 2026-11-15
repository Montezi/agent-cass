# Revisão AppSec do agent-cass — 15/11/2026

## Resultado e escopo

Revisão defensiva do código local e histórico acessível, com correções pequenas
para publicação da variante leve. **682 testes passaram, 11 testes Gemini
excluídos**, usando `.venv/bin/python -m pytest -q`. Não houve chamadas a modelos
pagos, deploy ou alteração de IAM/Secret Manager nesta revisão. Não foi construída
imagem Docker neste host. A configuração remota atual não foi inspecionada.

O código confirma POST `/api/agente`, POST `/api/agente/stream` e GET `/health`.
Também há as rotas padrão de documentação/OpenAPI do FastAPI. Na data da revisão,
não havia GET `/api/clima`, integração Meteomatics/Open-Meteo ou consumo de
`stats/<id>.json` neste backend; a previsão consultava `CLIMA_URL`, com fallback
para `forecast-snapshot.json`. Essas diferenças em relação ao texto de contexto
limitam o alcance da revisão. A GET `/api/clima` (Meteomatics → Open-Meteo →
snapshot) e a remoção da `CLIMA_URL` vieram depois e **não** foram cobertas por esta
revisão; o tratamento das credenciais está em Secret Manager, abaixo.

Referências: [OWASP Top 10:2025](https://owasp.org/Top10/2025/),
[OWASP API4:2023](https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/),
[OWASP LLM01:2025](https://genai.owasp.org/llmrisk/llm01-prompt-injection/).
Não são certificação de segurança; servem para classificar os riscos observados.

## Achados, correções e limites

### A1 — Renovação da cota por cabeçalho e abuso de custo

- **Severidade:** Alta.
- **Confiança:** Confirmada no código e em teste local; volume/custo remoto não medido.
- **Local:** `cass/limites.py:72`, `cass/seguranca_http.py:10`, `cass/agente.py:254`,
  `start.sh:12`, `cass/inicializacao.py:146`, seção 5 de `docs/deploy.md`.
- **Risco:** o primeiro X-Forwarded-For era controlável por quem chama. Trocar o
  endereço renovava a cota. Seis ferramentas não limitavam turnos por skills ou
  recusas; 2048 tokens eram um limite por chamada, não por pergunta. O serviço
  público podia sustentar chamadas pagas ao Gemini.
- **Correção aplicada:** a cota usa o peer do servidor, sem interpretar esse
  cabeçalho. Ambos os entrypoints desabilitam proxy headers no uvicorn. Há admissão
  compartilhada JSON/SSE de 4 pedidos simultâneos e 30 por minuto por processo,
  além dos 10 por minuto por peer. O Agent usa os limites abaixo e compartilha
  `resultado.invocation_context` com a tentativa de correção:

  ```python
  limits={
      "turns": 8,
      "output_tokens": 8192,
      "total_tokens": 60000,
      "structured_output_attempts": 3,
  }
  ```

  Foram testadas a negação da nona chamada, preservação do orçamento na correção,
  recusa de novos pedidos enquanto SSE ocupa vaga e liberação no cancelamento.
  O prazo total (hoje 35 s) agora inclui a conferência inicial do hotspot; leitura do corpo
  tem prazo próprio de 5 s.
- **Limites:** atrás de proxy, vários visitantes podem compartilhar o mesmo peer
  e a cota de 10/min. É obrigatório conferir isso na homologação; não reintroduzir
  confiança no primeiro XFF nem usar `FORWARDED_ALLOW_IPS=*` para contornar.
  As cotas são locais e reiniciam com o processo. Os limites de tokens do SDK
  dependem do uso reportado e são conferidos entre chamadas; não cancelam tokens
  já faturados. Threads de ferramentas já iniciadas podem continuar até seu timeout.

A configuração inicial recomendada é `--memory=2Gi --cpu=1 --max=2
--max-instances=2 --concurrency=4`, variante sem Decider. Máximo de instâncias
limita escala, **não gasto acumulado do Gemini**, e pode ser excedido temporariamente.
Alertas de orçamento também não interrompem gastos automaticamente.
[Cloud Run](https://docs.cloud.google.com/run/docs/configuring/max-instances),
[orçamentos](https://docs.cloud.google.com/billing/docs/how-to/budgets).

O MCP strands-agents não estava disponível nesta revisão. Os novos campos de
Limits e o compartilhamento de invocation_context foram conferidos no SDK 1.65.0
instalado, em testes com Agent real/modelo programado e na documentação oficial:
[Limits](https://strandsagents.com/docs/api/python/strands.types.agent/),
[Agent.invoke_async](https://strandsagents.com/docs/api/python/strands.agent.agent/).

### A2 — Campos publicados escapavam à validação de números e causalidade

- **Severidade:** Média.
- **Confiança:** Confirmada com reprodução determinística, sem precisar chamar Gemini.
- **Local:** `cass/verificacao.py:279`, `:304`, `:402`; publicação em `cass/contrato.py:78`.
- **Risco:** `opcoes=["Área inundada: 999 km²"]` passava na validação sem dados e
  era exibida pelo frontend. Opções e textos da hipótese/evidência não recebiam
  todas as verificações aplicadas a texto/medido.
- **Correção aplicada:** centralizei os campos publicados e submeti todos à mesma
  conferência numérica e causal. Nomes exatos de hotspots conhecidos podem conter
  dígitos; isso não libera esses números como medição em outras frases.

  ```python
  textos = [resposta.texto, *resposta.medido, *resposta.opcoes]
  if resposta.hipotese is not None:
      textos.extend([resposta.hipotese.texto, resposta.hipotese.evidencia])
  ```

- **Limite:** comparar valores não prova que unidade, relação causal ou contexto
  semântico estejam corretos. O teste positivo antigo que citava 2023 sem uma data
  nos dados foi ajustado para fornecer a evidência; não afrouxei a proteção.

### A3 — Leitura de respostas externas sem limite de tamanho

- **Severidade:** Média.
- **Confiança:** Confirmada; exploração exige upstream comprometido/errado, não
  uma URL escolhida livremente pelo usuário.
- **Local:** `cass/ferramentas.py:121`.
- **Risco:** baixar todo `.content` antes do parse podia consumir memória com
  resposta grande ou comprimida. Timeout por operação permite fluxos contínuos.
- **Correção aplicada:** leitura em streaming até 1 MiB, Content-Length conferido,
  `Accept-Encoding: identity`, recusa de codificação inesperada antes da
  descompressão, `follow_redirects=False` explícito e relógio conferido entre
  blocos. HTTP tem timeout de 5 s por operação. Erros continuam como dados e a
  previsão conserva o fallback.
- **Limite:** o prazo entre blocos não é um deadline rígido de parede da thread;
  uma leitura pode consumir seu timeout. Upstream que insistir em gzip será
  recusado: verificar o Hosting e o serviço de clima na homologação.

### A4 — Corpo lento ocupava atendimento sem prazo de leitura

- **Severidade:** Média.
- **Confiança:** Confirmada no middleware; efeito do buffering do proxy remoto
  precisa verificar.
- **Local:** `cass/limites.py:93`, `cass/seguranca_http.py:60`.
- **Risco:** enviar poucos bytes por vez mantinha a requisição aberta antes de
  começar o timeout do agente. O limite de 2 KiB sozinho não resolvia isso.
- **Correção aplicada:** prazo total de 5 s para o corpo, com HTTP 408; tamanho
  conferido antes de concatenar cada bloco; admissão limitada antes da leitura.
  Pedidos acima de 2 KiB continuam com 413. Testes cobrem leitura lenta e chunked.

### A5 — Mensagens de exceções do provedor podiam entrar no log

- **Severidade:** Média.
- **Confiança:** Confirmada quanto ao comportamento do logger; nenhum segredo
  real encontrado nos logs foi alegado.
- **Local:** `cass/main.py:79` e handler de erro SSE no mesmo arquivo.
- **Risco:** `logger.exception` inclui mensagem e traceback; exceção de biblioteca
  pode carregar pergunta, resposta ou detalhes internos em Cloud Logging.
- **Correção aplicada:** os handlers registram apenas mensagem fixa e nome da
  classe da exceção; a resposta ao cliente continua genérica. Teste com um marcador
  privado na exceção verifica que ele não aparece na resposta nem nesses logs.
- **Limite:** configurações de tracing, debug e logs próprios dos SDKs no ambiente
  publicado não foram auditadas. Hooks já registram apenas nomes, tempos e tipo.
- **Exceção (verificação da saída):** quando a camada final recusa uma resposta,
  `cass/verificacao.py` registra uma linha de aviso com o campo e o valor de cada
  número ou data sem apoio (`texto='2024'`, `medido[0]='999'`) e os nomes dos
  campos com causa afirmada. O valor é só o número ou a data que a verificação
  reconheceu, em uma linha, com até 40 caracteres e no máximo 10 por recusa. O
  texto da resposta e a pergunta nunca entram no log. Testes conferem campo,
  valor, limites e ausência do texto.

### A6 — pip vulnerável no ambiente de instalação

- **Severidade:** Média no contexto desta aplicação.
- **Confiança:** Confirmada para `.venv` com pip 24.0; versão da imagem pronta
  precisa verificar.
- **Local:** `Dockerfile:27`; metadados de pip do `.venv`.
- **Risco:** vulnerabilidades de instalação/extração de artefatos maliciosos.
  Não foi demonstrada exploração pela API HTTP do Cass.
- **Correção aplicada no build:** `python -m pip install --upgrade "pip==26.3"`
  antes das dependências. OSV não retornou advisory para essa versão. O `.venv`
  local foi preservado; atualizar seu pip antes de futuras instalações.

A consulta OSV cobriu 70 pacotes instalados, incluindo transitivos. Apenas pip
retornou avisos nesse ambiente: CVE-2025-8869, CVE-2026-1703, CVE-2026-3219,
CVE-2026-6357, CVE-2026-8643 e CVE-2026-13346 (há aliases duplicados).
A última correção listada entre esses avisos é 26.2.
[Evidência resumida](auditoria/dependencias-osv.json),
[advisory](https://github.com/advisories/GHSA-qwm4-qh6w-59xr),
[pip 26.3](https://pypi.org/project/pip/26.3/).
A ausência de avisos nessa consulta não certifica o ambiente nem a imagem.

### A7 — Dependências opcionais do Decider com advisories

- **Severidade:** Média no contexto do Cass; os advisories podem ter classificação
  maior quando o atacante controla um checkpoint.
- **Confiança:** versões afetadas confirmadas; caminho de exploração remota
  neste app precisa verificar e não foi encontrado nesta revisão.
- **Local:** `requirements-decider.txt:3` (`torch==2.7.1+cpu`) e `:9`
  (`accelerate==1.12.0`).
- **Risco:** PyTorch tem avisos relacionados a checkpoint não confiável e operações
  específicas. Accelerate tem aviso de traversal/DoS via `weight_map` de checkpoint.
- **Mitigação para hoje:** construir com `INCLUIR_DECIDER=false` e servir com
  `DECIDER_ATIVO=false`; assim esses pacotes não entram na imagem. Desativar apenas
  a variável de runtime numa imagem pesada não remove os pacotes vulneráveis.
- **Pendente antes de ativar:** atualizar versões compatíveis, conferir alcance
  dos avisos e repetir build e avaliação. Não fiz atualização cega de Torch que
  quebraria a combinação já medida. Modelo fixado/safetensors e ausência de upload
  de checkpoints reduzem a exposição, mas não substituem essa revisão.

[PyTorch CVE-2026-24747](https://github.com/advisories/GHSA-63cw-57p8-fm3p),
[Accelerate CVE-2026-69112](https://github.com/advisories/GHSA-4j2p-28q2-5m79).

### A8 — Entradas e dados de borda provocavam erros evitáveis

- **Severidade:** Baixa.
- **Confiança:** Confirmada com testes unitários/HTTP.
- **Local:** `cass/modelos.py:34`, `:204`, `cass/main.py:51`,
  `cass/ferramentas.py:301`.
- **Risco:** surrogate Unicode isolado podia falhar na codificação; 500 codepoints
  Python podiam ultrapassar 500 unidades UTF-16 do frontend; perguntas vazias
  gastavam processamento. `dias` aceitava booleanos/frações ou falhava com texto.
  Área/precipitação aceitavam infinito positivo e podiam invalidar a saída.
- **Correção aplicada:** pergunta exige texto útil e Unicode válido, contagem
  UTF-16; erros 422 não refletem o input arbitrário. `dias` exige inteiro real
  1–7; medições exigem finitude. Adicionei `nosniff` e `no-store` às respostas
  processadas pelo middleware, inclusive erros esperados e SSE. Não alego que
  cabeçalhos sozinhos resolvam XSS ou injeção de prompt.

### A9 — Injeção direta/indireta permanece risco residual

- **Severidade:** Média, considerando ferramentas só de leitura e sem segredos no prompt.
- **Confiança:** Provável; nenhum bypass novo do Gemini foi comprovado nesta revisão.
- **Local:** `cass/agente.py:89`, `cass/guardrails.py:72`, `:184`.
- **Risco:** regex, truncamento a 200 caracteres e `_tipo=dado` não impedem todo
  comando disfarçado. Um texto do GeoJSON pode influenciar a resposta; conteúdo
  fora do escopo/revelação do prompt não têm garantia determinística.
- **Correção auxiliar:** prompt reforçado para escopo, proteção de instruções
  internas e tratamento de ordens em dados como não confiáveis. A proteção real
  de capacidade continua na lista de ferramentas, validação de argumentos, cotas,
  fontes montadas pelo código e verificação da saída.
- **Pendente:** ensaios adversariais representativos com Gemini e revisão do
  pipeline de publicação dos dados. Não armazenar segredos em prompts/skills.

## Verificações sem vulnerabilidade confirmada

- **Segredos:** scan por padrões de 183 blobs no histórico acessível por
  `git rev-list --objects --all`, além de fontes atuais, não encontrou chave privada,
  token ou credencial hardcoded reconhecida. `.env`/ADC não estavam versionados.
  Isso não verifica commits apagados/inacessíveis, logs remotos ou todos os formatos.
- **SSRF/traversal:** ID é formato restrito e comparado à lista validada; lat/lon
  vêm do GeoJSON com faixas verificadas, não de parâmetros livres do solicitante.
  Queries usam `httpx.URL`. Testes confirmam IDs com URL/traversal sem troca de
  destino e redirects recusados. DADOS_BASE é configuração confiável. Meteomatics e
  Open-Meteo são URLs fixas no código, consultadas só com as coordenadas de um
  hotspot do GeoJSON: a `/api/clima` recusa lat/lon que não sejam as dele.
- **XSS:** `../cassaandra-alert-frontend/src/features/cass/CassAnswer.tsx:7`
  renderiza texto React e `<strong>` ao dividir `**`; não executa HTML nem Markdown
  arbitrário. Fontes/chips/nota também são texto. Não foi encontrado sink XSS
  nesse fluxo. Backend limita a resposta conforme unidades UTF-16 do Zod.
- **CORS:** apenas APP_ORIGIN; GET/POST, Content-Type/Accept, sem credenciais ou
  wildcard. CORS não autentica chamadas feitas fora do navegador.
- **Container/ADC:** `USER cass` (UID 10001), cópia explícita, credenciais fora do
  contexto; cliente Vertex por ADC, sem chave Gemini. Imagem e IAM remotos não
  verificados. python:3.12-slim é tag mutável; transitivos não têm lock/hashes.
- **Secret Manager:** este serviço lê METEOMATICS_USER/PASSWORD do Secret Manager
  com `--update-secrets` (`docs/deploy.md`, seções 3 e 5), nunca com
  `--set-env-vars`. A conta `cass-agente` recebe secretAccessor apenas nesses dois
  segredos; vinculações efetivas/herdadas ainda precisam ser conferidas remotamente.
  A senha vai à Meteomatics por Basic Auth (fora da URL), fica fora do repr da
  configuração e não aparece nos logs de falha.
- **Contrato:** pergunta 500, ID com formato/comprimento/lista, language pt/en,
  corpo 2 KiB e resposta limitada estavam parcialmente implementados; preservados
  e reforçados. 422 continua sendo decisão de contrato, não vulnerabilidade.

## Tabela-resumo por severidade

| ID | Severidade | Confiança | Estado |
|---|---|---|---|
| A1 | Alta | Confirmada | Corrigido no código; homologar IP/proxy e cotas reais |
| A2 | Média | Confirmada | Corrigido e testado |
| A3 | Média | Confirmada | Corrigido; conferir encoding dos upstreams |
| A4 | Média | Confirmada | Corrigido e testado |
| A5 | Média | Confirmada | Handlers corrigidos; logs/tracing remotos pendentes |
| A6 | Média | Confirmada local; imagem precisa verificar | Build atualizado; pip local antigo |
| A7 | Média | Versões confirmadas; exploração precisa verificar | Excluído da variante leve; variante Decider pendente |
| A9 | Média | Provável | Mitigado, sem promessa de eliminar prompt injection |
| A8 | Baixa | Confirmada | Corrigido e testado |

## Corrigir/validar antes de publicar hoje

- [x] Corrigir XFF, admissão agregada, limites do Agent e prazo do hotspot.
- [x] Corrigir campos da resposta sem validação, corpo lento, limites de download,
  validação de borda e logs da aplicação.
- [x] Rodar suíte local: **682 passed, 11 deselected**; `pip check` passou.
- [ ] Construir a variante leve e conferir pip 26.3, ausência de Torch, usuário
  sem root, `/health`, JSON e SSE. Fazer scan da imagem produzida.
- [ ] Homologar pelo Firebase e pela URL direta: IP/429/Retry-After, cancelamento
  SSE, 413/422/504 e CORS para `https://cassaandra-alert.web.app`.
- [ ] Conferir que a cota compartilhada do proxy atende à demonstração; se não,
  configurar uma cadeia de proxies comprovadamente confiável antes de publicar.
- [ ] Conferir DADOS_BASE, Meteomatics e Open-Meteo e que aceitam `Accept-Encoding: identity`.
- [ ] Validar `agente`, us-central1, 2 GiB/1 CPU, máximo de serviço/revisão 2,
  concorrência 4, DECIDER_ATIVO=false, conta cass-agente via ADC e menor privilégio.
- [ ] Conferir permissões herdadas e que os segredos Meteomatics chegam ao serviço
  `agente` só por `--update-secrets`, com secretAccessor por segredo. Não copiar
  ADC para a imagem.
- [ ] Definir alertas/cotas disponíveis da Vertex e responsável por interromper
  abuso. Guardar revisão estável para rollback.
- [ ] Executar smoke/bateria Gemini após build, com dados de exemplo e sem tratar
  resultados sintéticos como medições científicas. Não foi executado nesta revisão.

## Pode ficar para depois do hackathon

1. Lock/hashes de transitivos, digest de base, SBOM e scans automatizados no CI.
2. Cota distribuída/identidade de cliente mais robusta se a escala justificar.
3. Ampliar avaliação adversarial, semântica de medidas e governança dos dados.
4. Atualizar/reavaliar dependências do Decider antes de considerar sua ativação.
5. Revisar o serviço clima/pipeline em seus próprios repositórios e reconciliar
   contratos pendentes. Isso não autoriza afirmar que esses serviços já foram auditados.
