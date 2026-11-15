# Deploy do Cass no Cloud Run (passo 10)

Serviço público para o frontend, com Strands e Gemini na Vertex AI. A imagem usa
`python:3.12-slim` e inclui somente runtime, `cass/`, `skills/` e `start.sh`.
O Decider fica desligado: não há PyTorch nem download de modelo local.

`INCLUIR_DECIDER=false` é o argumento de build reservado para o passo 11. Nesta
fase, outro valor interrompe o build, pois essa variante ainda não existe. O
Dockerfile também confere que PyTorch não foi instalado. As versões das
dependências diretas de runtime foram conferidas no `.venv`; testes ficam em
`requirements-dev.txt` e não são instalados na imagem.

Os comandos rodam na raiz do repositório, no Cloud Shell ou em um terminal com
Google Cloud CLI autenticado e projeto com faturamento habilitado. O deploy por
`--source .` usa o Dockerfile presente e dispensa Docker local.
[Referência: deploy por código-fonte](https://docs.cloud.google.com/run/docs/deploying-source-code).

## 1. Definir as variáveis

```bash
PROJECT_ID="seu-projeto"
REGIAO="us-central1"               # região do serviço Cloud Run
SERVICO="agent-cass"
CONTA="cass-agente@${PROJECT_ID}.iam.gserviceaccount.com"
GOOGLE_CLOUD_LOCATION="global"    # localização do Gemini na Vertex AI
GEMINI_MODEL="gemini-3.8-flash"

# URLs públicas; localhost no Cloud Run não aponta para sua máquina.
APP_ORIGIN="https://seu-site.web.app"
DADOS_BASE="${APP_ORIGIN}/dados"
CLIMA_URL="${APP_ORIGIN}/api/clima"

gcloud auth login
gcloud config set project "$PROJECT_ID"
```

No Cloud Shell, a autenticação costuma já estar configurada. Para atualizar o
ensaio privado, use a região do serviço existente. `APP_ORIGIN` contém somente
a origem do frontend, sem caminho ou barra final.

## 2. Ativar as APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  iam.googleapis.com \
  --project "$PROJECT_ID"
```

## 3. Preparar as contas de serviço

A conta de execução do Cass chama a Vertex AI por ADC, sem chave de API. Crie
uma vez; se `cass-agente` já existir do ensaio anterior, reutilize-a.

```bash
gcloud iam service-accounts create cass-agente \
  --display-name="Cass (agent-cass no Cloud Run)" \
  --project "$PROJECT_ID"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CONTA}" \
  --role="roles/aiplatform.user"
```

A conta de build é separada. Consulte a conta padrão do projeto e atribua a ela
`roles/run.builder`, sem presumir qual conta o projeto usa:

```bash
CONTA_BUILD="$(gcloud builds get-default-service-account \
  --project "$PROJECT_ID" --region "$REGIAO")"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CONTA_BUILD}" \
  --role="roles/run.builder"
```

Quem faz source deploy precisa de `roles/run.sourceDeveloper` e
`roles/serviceusage.serviceUsageConsumer` no projeto, além de
`roles/iam.serviceAccountUser` sobre a conta de execução. Ativar APIs, criar
contas e conceder papéis requer também as permissões administrativas
correspondentes. Tornar o serviço público com
`--allow-unauthenticated` exige `run.services.setIamPolicy`, incluída em
`roles/run.admin`. Se faltar permissão, o administrador do projeto pode concedê-la.
[Conta de build](https://docs.cloud.google.com/run/docs/configuring/services/build-service-account),
[acesso público](https://docs.cloud.google.com/run/docs/authenticating/public).

## 4. Guardar a revisão atual antes de atualizar

Se o serviço já existir, identifique a revisão estável que recebe tráfego e
guarde seu nome. No primeiro deploy, pule este passo.

```bash
gcloud run services describe "$SERVICO" --region "$REGIAO" \
  --format='yaml(status.traffic)'

REVISAO_ANTERIOR="nome-da-revisao-estavel-que-recebe-trafego"
```

Use `status.traffic`: a última revisão criada pode ser uma versão já retirada
de tráfego num rollback anterior.

## 5. Fazer o deploy público

```bash
gcloud run deploy "$SERVICO" \
  --project "$PROJECT_ID" \
  --source . \
  --region "$REGIAO" \
  --service-account "$CONTA" \
  --memory=2Gi \
  --max-instances=2 \
  --timeout=60 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION},GEMINI_MODEL=${GEMINI_MODEL},APP_ORIGIN=${APP_ORIGIN},DADOS_BASE=${DADOS_BASE},CLIMA_URL=${CLIMA_URL},DECIDER_ATIVO=false,DECIDER_URL=http://127.0.0.1:8099/v1/systemone"
```

O Cloud Build constrói a imagem e a guarda no Artifact Registry. No primeiro
deploy, o CLI pode pedir para criar o repositório `cloud-run-source-deploy`.
`.gcloudignore` exclui `.env`, `.venv`, testes e configurações pessoais do envio.
O build usa `INCLUIR_DECIDER=false` do Dockerfile, sem flag adicional no Cloud Run.

`--allow-unauthenticated` permite chamadas sem token, inclusive ao atualizar o
ensaio privado. `PORT` é injetada pelo Cloud Run: `start.sh` escuta em `0.0.0.0`
nessa porta, com padrão 8080. Não defina `GOOGLE_APPLICATION_CREDENTIALS` no
serviço: o ADC usa a conta de execução. O limite HTTP é 60 s; o Cass mantém seu
próprio prazo de 20 s.
[Opções de deploy](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy).

Se um rollback anterior deixou o tráfego fixado em uma revisão, promova a nova
revisão explicitamente:

```bash
gcloud run services update-traffic "$SERVICO" --region "$REGIAO" --to-latest
```

Para atualizar outra vez, registre a revisão estável e repita o deploy.

## 6. Conferir o serviço publicado

```bash
URL="$(gcloud run services describe "$SERVICO" --region "$REGIAO" --format='value(status.url)')"

curl -fsS "$URL/health"
# {"ok":true}

curl -fsS -X POST "$URL/api/agente" \
  -H 'Content-Type: application/json' \
  --data '{"question":"O que mudou no Guaíba?","hotspot_id":"guaiba-rs","language":"pt"}'
# paragraphs, tools, sources e note, conforme docs/api-contract.md

curl -N -fsS -X POST "$URL/api/agente/stream" \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  --data '{"question":"O que mudou no Guaíba?","hotspot_id":"guaiba-rs","language":"pt"}'
# pensando, ferramenta e resposta; falhas após abrir SSE chegam no evento erro.
```

Configure os rewrites do Firebase Hosting para os caminhos do agente; o frontend
continua usando caminhos relativos. O stream é adicional e sua adoção está em
[`propostas-api-contract.md`](propostas-api-contract.md). As ferramentas precisam
alcançar `DADOS_BASE` e `CLIMA_URL`; previsão indisponível deve aparecer como
limitação na resposta.

CORS permite somente `APP_ORIGIN`. JSON e SSE compartilham o limite local de
10 perguntas/min por IP, com `Retry-After` no HTTP 429. A cota é por processo,
não global entre instâncias. Confira a identidade de cliente entregue pelo proxy
na integração publicada. [Detalhes das camadas de proteção](guardrails.md).

## 7. Consultar os logs

```bash
gcloud run services logs read "$SERVICO" --region "$REGIAO" --limit=50
```

Cada pergunta registra uma linha JSON com ferramentas, tempos e tipo. Falhas
aparecem como `o agente falhou` (500) ou `o agente passou do tempo limite` (504).

## 8. Voltar para a revisão anterior

Liste as revisões se precisar recuperar o nome guardado antes do deploy:

```bash
gcloud run revisions list --service "$SERVICO" --region "$REGIAO"

gcloud run services update-traffic "$SERVICO" --region "$REGIAO" \
  --to-revisions="${REVISAO_ANTERIOR}=100"

gcloud run services describe "$SERVICO" --region "$REGIAO" \
  --format='yaml(status.traffic)'

curl -fsS "$URL/health"
```

O comando direciona todo o tráfego à revisão escolhida, incluindo seu código e
suas variáveis. As permissões de acesso público e os papéis IAM continuam no
nível do serviço/projeto.
[Referência: rollback e migração de tráfego](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration).

## Testar o container localmente

Com Docker instalado:

```bash
docker build --build-arg INCLUIR_DECIDER=false -t agent-cass:local .

# Confere a ausência de PyTorch sem chamar o Gemini.
docker run --rm --entrypoint python agent-cass:local \
  -c "from importlib.util import find_spec; assert find_spec('torch') is None"

# /health exige configuração do projeto, mas não chama a Vertex AI.
docker run --rm --name agent-cass-local -p 8080:8080 \
  -e GOOGLE_CLOUD_PROJECT=projeto-teste \
  -e DECIDER_ATIVO=false \
  agent-cass:local
```

Em outro terminal:

```bash
curl -fsS http://localhost:8080/health
docker stop agent-cass-local
```

Para uma pergunta real no Linux, configure ADC e sirva os dados locais:

```bash
gcloud auth application-default login
python3 -m http.server 8765 --bind 127.0.0.1 --directory tests/fixtures
```

Em outro terminal, rode o container. `--network host` alcança o servidor local,
e `--user` permite ler o ADC do seu usuário. A montagem é somente para leitura e
não altera a imagem:

```bash
docker run --rm --network host \
  --user "$(id -u):$(id -g)" \
  --env-file .env \
  -e PORT=8082 \
  -e DECIDER_ATIVO=false \
  -e DADOS_BASE=http://127.0.0.1:8765 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/adc.json \
  -v "$HOME/.config/gcloud/application_default_credentials.json:/adc.json:ro" \
  agent-cass:local
```

```bash
curl -fsS -X POST http://localhost:8082/api/agente \
  -H 'Content-Type: application/json' \
  --data '{"question":"O que mudou no Guaíba?","hotspot_id":"guaiba-rs","language":"pt"}'
```
