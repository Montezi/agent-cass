# Deploy de ensaio no Cloud Run (privado)

Um serviço privado (`--no-allow-unauthenticated`) para testar o agent-cass na nuvem
antes do deploy público do passo 10. Sem o Strands Decider (`DECIDER_ATIVO=false`).
Só quem tem um token de identidade com `roles/run.invoker` consegue chamar.

Os comandos rodam no Cloud Shell ou num terminal com o `gcloud` autenticado
(`gcloud auth login`), na raiz do repositório.

## 0. Variáveis

```bash
PROJECT_ID="seu-projeto"          # o mesmo do GOOGLE_CLOUD_PROJECT
REGIAO="us-central1"              # região do Cloud Run (a do Gemini é outra: global)
SERVICO="agent-cass"
CONTA="cass-agente@${PROJECT_ID}.iam.gserviceaccount.com"

# De onde o Cass lê os dados. Precisa ser público: localhost não existe no Cloud Run.
APP_ORIGIN="https://seu-site.web.app"
DADOS_BASE="${APP_ORIGIN}/dados"
CLIMA_URL="${APP_ORIGIN}/api/clima"

gcloud config set project "$PROJECT_ID"
```

Se o frontend ainda não estiver publicado, o ensaio ainda testa a autenticação, o
Gemini e o contrato: as ferramentas devolvem erro de leitura e o Cass diz que não
conseguiu ler os dados.

## 1. Ativar as APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com
```

## 2. Conta de serviço do Cass

No Cloud Run, a conta de serviço faz o papel do ADC: o Cass chama o Gemini na
Vertex AI com ela, sem chave de API.

```bash
gcloud iam service-accounts create cass-agente \
  --display-name="Cass (agent-cass no Cloud Run)"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CONTA}" \
  --role="roles/aiplatform.user"
```

Para fazer o deploy com essa conta, quem roda o deploy precisa poder agir como ela
(`roles/iam.serviceAccountUser`). O Owner do projeto já pode.

## 3. Deploy

O `--source .` manda o código para o Cloud Build, que constrói a imagem a partir do
`Dockerfile`. O `.gcloudignore` deixa o `.env`, o `.venv` e os testes de fora do envio.

```bash
gcloud run deploy "$SERVICO" \
  --source . \
  --region "$REGIAO" \
  --service-account "$CONTA" \
  --no-allow-unauthenticated \
  --memory=2Gi \
  --max-instances=2 \
  --timeout=60 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GEMINI_MODEL=gemini-3.8-flash,APP_ORIGIN=${APP_ORIGIN},DADOS_BASE=${DADOS_BASE},CLIMA_URL=${CLIMA_URL},DECIDER_ATIVO=false"
```

- `--max-instances=2` limita o custo do ensaio.
- `--timeout=60` é o mesmo corte do Firebase Hosting. O próprio Cass desiste em 20 s
  e devolve 504.
- No primeiro deploy, o `gcloud` pergunta se pode criar o repositório
  `cloud-run-source-deploy` no Artifact Registry: responda `Y`.
- Se o build falhar com permissão negada, dê à conta que o Cloud Build usa (em
  projetos novos, a conta padrão do Compute Engine) o papel `roles/run.builder`:

  ```bash
  NUMERO=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${NUMERO}-compute@developer.gserviceaccount.com" \
    --role="roles/run.builder"
  ```

## 4. Testar com curl

O serviço é privado: cada chamada leva um token de identidade da sua conta. O Owner
do projeto já tem permissão de chamar; para outra pessoa:

```bash
gcloud run services add-iam-policy-binding "$SERVICO" --region "$REGIAO" \
  --member="user:EMAIL_DA_PESSOA" --role="roles/run.invoker"
```

```bash
URL=$(gcloud run services describe "$SERVICO" --region "$REGIAO" --format='value(status.url)')
TOKEN=$(gcloud auth print-identity-token)

# Sem token: o Cloud Run recusa (403).
curl -s -o /dev/null -w "%{http_code}\n" "$URL/health"

# Com token:
curl -s -H "Authorization: Bearer $TOKEN" "$URL/health"
# {"ok":true}

curl -s -X POST "$URL/api/agente" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question": "O que mudou no Guaíba?", "hotspot_id": "guaiba-rs", "language": "pt"}'
# {"paragraphs": [...], "tools": ["consultar_hotspot"], "sources": [...], "note": ...}
```

O token vale por cerca de 1 hora; depois, gere outro.

## 5. Logs

```bash
gcloud run services logs read "$SERVICO" --region "$REGIAO" --limit=50
```

Falhas do agente aparecem como `o agente falhou` (500) ou `o agente passou do tempo
limite` (504).

## 6. Voltar para a revisão anterior

```bash
gcloud run revisions list --service "$SERVICO" --region "$REGIAO"
gcloud run services update-traffic "$SERVICO" --region "$REGIAO" \
  --to-revisions=NOME_DA_REVISAO=100
```

## 7. Apagar o ensaio

```bash
gcloud run services delete "$SERVICO" --region "$REGIAO"
gcloud iam service-accounts delete "$CONTA"   # só se não for usar no passo 10
```

## Teste local do container (antes do deploy)

Precisa do Docker. Use `--network host` (Linux) para o container enxergar dados
servidos na máquina, e o ADC local no lugar da conta de serviço. O `--user` faz o
container ler o arquivo de credenciais, que só o seu usuário pode ler.

```bash
docker build -t agent-cass:ensaio .

# O .env não entra na imagem:
docker run --rm --entrypoint ls agent-cass:ensaio -la /app

# Dados de exemplo servidos na máquina (outro terminal):
python -m http.server 8765 --bind 127.0.0.1 --directory tests/fixtures

docker run --rm --network host \
  --user "$(id -u):$(id -g)" \
  --env-file .env \
  -e PORT=8082 \
  -e DADOS_BASE=http://127.0.0.1:8765 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/adc.json \
  -v "$HOME/.config/gcloud/application_default_credentials.json:/adc.json:ro" \
  agent-cass:ensaio

curl -s localhost:8082/health
curl -s -X POST localhost:8082/api/agente -H "Content-Type: application/json" \
  -d '{"question": "O que mudou no Guaíba?", "hotspot_id": "guaiba-rs", "language": "pt"}'
```

## Antes do deploy público (passo 10)

O Firebase Hosting não manda token de identidade, então o rewrite de `/api/agente`
só funciona com o serviço público (`--allow-unauthenticated`). Antes de abrir,
entram as camadas do passo 9 (limite por IP, tamanho da mensagem, CORS só para o
`APP_ORIGIN`).
