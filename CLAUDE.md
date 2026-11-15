# agent-cass

Agente **Cass** do app Alerta CassAAndra (NASA Space Apps Challenge 2026, desafio
"Dancing with the SARs"). O Cass explica, em PT e EN, o que o radar NISAR mediu de
mudança na superfície em hotspots sensíveis ao Super El Niño.

Serviço Python 3.12: **Strands Agents** (o agente) + **Gemini na Vertex AI** (o modelo)
+ **FastAPI** (só a camada HTTP). Deploy no **Google Cloud Run**.

O passo a passo de construção está em `docs/plano-de-desenvolvimento.md`.
Trabalhe um passo por vez e não adiante passos que não foram pedidos.

## Regras que nunca mudam

1. **Strands em Python.** O FastAPI só recebe a requisição e chama o agente.
2. **Modelo:** `GeminiModel(client=genai.Client(vertexai=True, project=..., location=...))`.
   Nunca `BedrockModel`, nunca chave de API do Gemini. A autenticação é por ADC
   (local: `gcloud auth application-default login`; no Cloud Run: conta de serviço).
3. **Confira antes de usar.** Antes de escrever código com qualquer API do Strands
   (`Agent`, `@tool`, interventions, steering, hooks, `AgentSkills`, structured output),
   confirme o import e a assinatura no MCP `strands-agents` (`search_docs`, `fetch_doc`).
   Se a documentação não confirmar, diga isso em vez de inventar.
4. **Ferramentas do Cass são só de leitura.** Montam a URL a partir de `DADOS_BASE`
   e de um `id` validado contra a lista de `hotspots.geojson`. A previsão também lê
   `api.meteomatics.com` e `api.open-meteo.com`, só com as coordenadas de um hotspot
   do `hotspots.geojson` (nunca um ponto vindo do pedido ou do modelo).
   Proibido dar ao Cass: `file_read`, `shell`, busca na web, escrita em disco ou rede
   fora desses destinos.
5. **Contrato de dados:** `hotspots.geojson` segue `docs/data-contract.md` (chaves em
   inglês: `dominant_class`, `flood`, `affected_area_km2`...). O espelho Pydantic fica em
   `cass/modelos.py`. Valores medidos podem ser `null`: nunca invente número.
   Se `metadata.sample` for `true`, o Cass avisa que os dados são de exemplo.
6. **Saída:** o `POST /api/agente` segue `docs/api-contract.md`, que é o formato público e
   vale sobre o apêndice A. Por dentro, o agente produz `RespostaCass` (apêndice A) e
   `cass/contrato.py` converte para o contrato.
7. **Harness:** Skills = conhecimento sob demanda; Interventions = regras duras
   (Deny/Transform); `SteeringCass` = correção de rumo (Guide); Hooks = só observar.
8. **Decider:** só por `decider_sim()`, que respeita `DECIDER_ATIVO` e devolve um valor
   padrão seguro quando o Decider está desligado, lento ou fora do ar.
9. **Medido × Hipótese:** o Cass nunca afirma que o El Niño causou uma mudança;
   isso é hipótese, com evidência e força (fraca, moderada, forte).
10. **Segredos** só em variáveis de ambiente. `.env` nunca vai para o git.
11. **Testes:** toda mudança vem com teste em `tests/`. Rode `pytest -q` e mostre o
    resultado antes de dizer que terminou. Testes que chamam o Gemini de verdade levam
    `@pytest.mark.gemini` e ficam fora da execução padrão.

## Estrutura

```text
agent-cass/
├── cass/
│   ├── __init__.py
│   ├── config.py        # lê as variáveis de ambiente
│   ├── main.py          # FastAPI: /health, /api/agente e /api/clima
│   ├── agente.py        # monta o Agent do Strands
│   ├── contrato.py      # RespostaCass -> formato de docs/api-contract.md
│   ├── formato.py       # datas, números e fontes em texto (igual ao frontend)
│   ├── modelos.py       # Pydantic: contrato de dados, PedidoAgente/RespostaAgente, RespostaCass
│   ├── ferramentas.py   # as 8 ferramentas (@tool) e a cadeia da previsão (/api/clima)
│   ├── guardrails.py    # decider_sim, PortaoDeFerramentas, LimpaDados, camadas 1 e 4
│   ├── steering.py      # SteeringCass
│   └── hooks.py         # ProgressoERegistro
├── skills/              # skills do Cass (runtime), uma pasta por skill
├── tests/
│   └── fixtures/        # hotspots.geojson mock (metadata.sample = true)
├── docs/
│   ├── adr/             # ADR-AGT-001 em diante
│   ├── data-contract.md # cópia do contrato do repositório do frontend
│   └── plano-de-desenvolvimento.md
├── requirements.txt  requirements-dev.txt  Dockerfile  start.sh  .env.example
```

## Comandos

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt                           # serviço + testes (a imagem só usa o requirements.txt)
uvicorn cass.main:app --reload --port 8080 --env-file .env   # rodar local
pytest -q                                                     # testes rápidos
pytest -q -m gemini                                           # testes com o Gemini
```

## Estilo

- Código, comentários e mensagens de commit em português; chaves do contrato em inglês.
- Funções pequenas, com type hints. Erros de ferramenta voltam como dado
  (`{"erro": ..., "validos": [...]}`), para o Gemini se corrigir.
