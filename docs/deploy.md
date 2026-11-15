# Deploy do Cass no Cloud Run

Serviço público para o frontend, com Strands e Gemini na Vertex AI. A imagem usa
`python:3.12-slim` e inclui somente runtime, `cass/`, `skills/` e `start.sh`.
Na variante padrão, o Decider fica desligado: não há PyTorch nem download de
modelo local. Os comandos de Cloud Run abaixo continuam usando essa variante.

`INCLUIR_DECIDER=false` produz a imagem leve; `true` acrescenta PyTorch CPU, o
servidor e os pesos fixados do Decider. O Dockerfile confere que PyTorch não foi
instalado na variante leve e que a variante com modelo não tem CUDA. As versões das
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
SERVICO="agente"                    # nome esperado pelo rewrite do frontend
CONTA="cass-agente@${PROJECT_ID}.iam.gserviceaccount.com"
GOOGLE_CLOUD_LOCATION="global"    # localização do Gemini na Vertex AI
GEMINI_MODEL="gemini-3.8-flash"

# URLs públicas; localhost no Cloud Run não aponta para sua máquina.
APP_ORIGIN="https://seu-site.web.app"
DADOS_BASE="${APP_ORIGIN}/dados"

gcloud auth login
gcloud config set project "$PROJECT_ID"
```

No Cloud Shell, a autenticação costuma já estar configurada. Para atualizar o
ensaio privado, use a região do serviço existente. `APP_ORIGIN` contém somente
a origem do frontend, sem caminho ou barra final.
O frontend existente encaminha `/api/agente` para o serviço `agente`; se publicar
com outro nome, ajuste o rewrite em conjunto. Serviços de ensaios anteriores
podem ter nomes diferentes. Ver [`fluxo-de-dados.md`](fluxo-de-dados.md).

## 2. Ativar as APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  iam.googleapis.com \
  secretmanager.googleapis.com \
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

### Credenciais da Meteomatics (opcional)

A `GET /api/clima` e o `previsao_tempo` usam a Meteomatics só quando
`METEOMATICS_USER` e `METEOMATICS_PASSWORD` existem; sem elas, a previsão começa
na Open-Meteo, que não tem chave. As duas vêm do Secret Manager, nunca de
`--set-env-vars`, do Dockerfile ou do histórico do shell. Crie os segredos uma vez;
o `read -rs` não mostra o valor nem o grava no histórico:

```bash
read -rs -p "Usuário da Meteomatics: " VALOR && echo
printf %s "$VALOR" | gcloud secrets create meteomatics-user --data-file=- --project "$PROJECT_ID"
read -rs -p "Senha da Meteomatics: " VALOR && echo
printf %s "$VALOR" | gcloud secrets create meteomatics-password --data-file=- --project "$PROJECT_ID"
unset VALOR

for SEGREDO in meteomatics-user meteomatics-password; do
  gcloud secrets add-iam-policy-binding "$SEGREDO" \
    --member="serviceAccount:${CONTA}" \
    --role="roles/secretmanager.secretAccessor" \
    --project "$PROJECT_ID"
done
```

A conta de execução recebe `roles/secretmanager.secretAccessor` só nesses dois
segredos, não no projeto. O deploy (seção 5) liga os segredos às variáveis com
`--update-secrets`. O Cloud Run lê o valor na subida da instância: para trocar a
senha, crie uma versão nova (`gcloud secrets versions add meteomatics-password
--data-file=-`) e faça um deploy novo. Sem conta na Meteomatics, pule esta parte e
tire a linha `--update-secrets` dos comandos de deploy.
[Segredos no Cloud Run](https://docs.cloud.google.com/run/docs/configuring/services/secrets).

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
  --cpu=1 \
  --cpu-throttling \
  --cpu-boost \
  --concurrency=4 \
  --max=2 \
  --max-instances=2 \
  --timeout=60 \
  --allow-unauthenticated \
  --update-secrets=METEOMATICS_USER=meteomatics-user:latest,METEOMATICS_PASSWORD=meteomatics-password:latest \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION},GEMINI_MODEL=${GEMINI_MODEL},APP_ORIGIN=${APP_ORIGIN},DADOS_BASE=${DADOS_BASE},DECIDER_ATIVO=false,DECIDER_URL=http://127.0.0.1:8099/v1/systemone"
```

Esta é a variante leve, sem o Decider: um só processo, que espera o Gemini a
maior parte do tempo. A configuração inicial é `--memory=2Gi --cpu=1`. Com
`--cpu-throttling` explícito, o Cloud Run aloca CPU durante o atendimento de um
pedido, inclusive durante o stream SSE; o `--cpu-boost` dá CPU extra na subida a
frio. Os 8 GiB, as 4 CPUs e o `--no-cpu-throttling` são da variante com Decider
(seção abaixo), que mantém um modelo carregado em memória.

**Aquecimento das previsões desligado.** Com `--cpu-throttling`, o Cloud Run só
dá CPU à instância enquanto ela atende um pedido. Um trabalho em segundo plano
iniciado na subida, como buscar a previsão dos seis hotspots para encher o cache,
fica praticamente sem CPU assim que a subida termina: demora a acabar, segura
conexões abertas e não garante cache cheio. Por isso o aquecimento só roda com
`AQUECER_PREVISOES=true`, e o padrão é `false` (a variável fica fora do
`--set-env-vars` acima). Sem ele, o primeiro pedido de cada hotspot paga a busca
(até cerca de 6 s quando só o snapshot responde, dentro dos 8 s do app), e os
seguintes acham o cache: 30 min para Meteomatics ou Open-Meteo, 60 s para o
snapshot. Só ligue o aquecimento numa revisão com `--no-cpu-throttling`, como a
variante com Decider.

Para a demonstração pública, `--max=2` limita a escala no nível do serviço,
`--max-instances=2` também limita a revisão e `--concurrency=4` admite até quatro
pedidos simultâneos por instância. É um ponto inicial conservador: medir latência
e recusas antes de aumentar. O limite padrão de concorrência do Cloud Run seria
muito maior. A aplicação admite no máximo quatro perguntas simultâneas e trinta
por minuto por processo, além da cota por IP. Essas cotas são locais: escalar ou
reiniciar instâncias muda o orçamento agregado; não representam uma cota global.

**Esses parâmetros não são um teto financeiro para o Gemini.** Mesmo uma instância
pode continuar chamando a Vertex AI durante todo o dia; o máximo de instâncias
também admite excessos temporários da plataforma. Defina alertas de orçamento,
monitore requisições/tokens e confira as cotas disponíveis para o modelo/projeto
antes de publicar. Orçamentos configurados apenas como alertas não interrompem o
gasto automaticamente. Para interromper abuso, retire o acesso público ou faça
rollback/desative a função; não espere que CORS ou o limite de instâncias encerrem
a cobrança da Vertex AI.
[Máximo de instâncias](https://docs.cloud.google.com/run/docs/configuring/max-instances),
[concorrência](https://docs.cloud.google.com/run/docs/about-concurrency),
[orçamentos e alertas](https://docs.cloud.google.com/billing/docs/how-to/budgets).

O Cloud Build constrói a imagem e a guarda no Artifact Registry. No primeiro
deploy, o CLI pode pedir para criar o repositório `cloud-run-source-deploy`.
`.gcloudignore` exclui `.env`, `.venv`, testes e configurações pessoais do envio.
O build usa `INCLUIR_DECIDER=false` do Dockerfile, sem flag adicional no Cloud Run.

`--allow-unauthenticated` permite chamadas sem token, inclusive ao atualizar o
ensaio privado. `PORT` é injetada pelo Cloud Run: `start.sh` escuta em `0.0.0.0`
nessa porta, com padrão 8080. Não defina `GOOGLE_APPLICATION_CREDENTIALS` no
serviço: o ADC usa a conta de execução. O limite HTTP é 60 s; o Cass mantém seu
próprio prazo de 35 s, e o app espera até 45 s.
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

curl -fsS "$URL/api/clima?hotspot=guaiba-rs&lat=-30.0&lon=-51.2"
# source, updated_at e days; "source":"meteomatics" confirma que os segredos chegaram

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
continua usando caminhos relativos. O `firebase.json` do frontend manda
`/api/clima` para um serviço `clima`: para o app usar a `/api/clima` deste
serviço, aponte esse rewrite para `agente` (ou publique esta mesma imagem também
como `clima`). O stream é adicional e sua adoção está em
[`propostas-api-contract.md`](propostas-api-contract.md). As ferramentas precisam
alcançar `DADOS_BASE`, `api.meteomatics.com` e `api.open-meteo.com`; previsão indisponível deve aparecer como
limitação na resposta.

CORS permite somente `APP_ORIGIN`. JSON e SSE compartilham o limite local de
10 perguntas/min por IP, com `Retry-After` no HTTP 429. A aplicação usa o endereço
da conexão, ignora o `X-Forwarded-For` recebido sem validação e o uvicorn inicia
com `--no-proxy-headers`. Atrás do Firebase/Cloud Run, vários visitantes podem
compartilhar o IP do proxy e, portanto, a mesma cota. Confirme esse comportamento
na publicação. Para recuperar o IP individual, é necessário validar a cadeia de
proxies confiáveis e suas regras de sobrescrita antes de alterar a configuração;
não libere confiança em qualquer origem nem no primeiro item do cabeçalho.
[Detalhes das camadas de proteção](guardrails.md).

### Conferências de segurança antes da publicação

- Confira a revisão efetiva, `DECIDER_ATIVO=false`, a conta de execução
  `cass-agente`, os limites de instâncias/concorrência e o rewrite do Firebase.
- A conta de execução usa ADC e `roles/aiplatform.user`; não deve receber os
  papéis de administrador usados por quem faz o deploy. Revise permissões
  herdadas no projeto, pois o comando acima não remove concessões antigas.
- O serviço `agente` serve a `GET /api/clima` e lê `METEOMATICS_USER` e
  `METEOMATICS_PASSWORD` do Secret Manager, com `--update-secrets` (seções 3 e 5).
  A conta `cass-agente` recebe `roles/secretmanager.secretAccessor` **somente
  nesses dois segredos**. Não coloque valores em `--set-env-vars`, no Dockerfile
  ou no histórico do shell.
- Gere a imagem leve e confira suas dependências/sistema operacional antes do
  deploy. A revisão do código não substitui o scanner da imagem que será publicada.
- Teste uma chamada legítima pelo Firebase e confirme que o modo demo do frontend
  não está escondendo falhas do backend. Confira também 413, 422, 429 e a política
  de IP, sem imprimir perguntas, tokens ou credenciais nos logs.

[Referência de segredos no Cloud Run](https://docs.cloud.google.com/run/docs/configuring/services/secrets).

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

## Variante opcional com Decider CPU (passo 11)

```bash
docker build --build-arg INCLUIR_DECIDER=true -t agent-cass:decider .

docker run --rm --entrypoint python agent-cass:decider \
  -c "import torch; assert torch.version.cuda is None; print(torch.__version__)"

# Configuração mínima para conferir a inicialização; /health não chama Gemini.
docker run --rm --name agent-cass-decider -p 8080:8080 \
  -e GOOGLE_CLOUD_PROJECT=projeto-teste \
  -e DECIDER_ATIVO=true \
  agent-cass:decider
```

O build instala `torch==2.7.1+cpu` pelo índice oficial CPU e as dependências de
[`requirements-decider.txt`](../requirements-decider.txt). O servidor vem do
commit `79bf6e7184c166990eec79034667cc0bfd445e12`: o release PyPI 0.3.0 não tem a
opção `--max-concurrency` usada para limitar a inferência.

[`scripts/baixar_modelo_decider.py`](../scripts/baixar_modelo_decider.py) baixa
`StrandsAgents/strands-decider-2B-qwen3.5-v1-2610` na revisão
`216d1ccb46a9e9b036eb9187b248db98480f77d4`. Esse checkpoint contém LoRA e head;
o build também baixa `Qwen/Qwen3.5-2B-Base` na revisão
`b1485b2fa6dfa1287294f269f5fb618e03d52d7c`. O manifesto fica em
`/opt/decider-manifest.json`, com cache em `/opt/decider-cache` e checkpoint local
em `/opt/decider-model`. O processo de inferência usa os arquivos offline.

A imagem com modelo ativa o Decider por padrão. `DECIDER_ATIVO=false` permite
rodar essa mesma imagem sem carregar os pesos. Ativar o Decider numa imagem leve
interrompe a subida com uma mensagem de configuração inválida.

Com o Decider ativo, `start.sh` executa o supervisor `cass.inicializacao`, que:

1. Inicia o Decider em `127.0.0.1:8099`, CPU, lote e concorrência de inferência 1.
2. Aguarda `/health` confirmar o checkpoint esperado e dispositivo CPU.
3. Inicia `uvicorn cass.main:app --host 0.0.0.0 --port ${PORT:-8080}`.
4. Encaminha sinais aos dois processos e encerra ambos se um deles terminar.

Os prazos padrão são `DECIDER_STARTUP_TIMEOUT_S=240` e
`DECIDER_SHUTDOWN_TIMEOUT_S=10`. `DECIDER_URL` permanece
`http://127.0.0.1:8099/v1/systemone`; a porta interna não precisa ser publicada.

O deploy usa os parâmetros solicitados: `--memory=8Gi --cpu=4
--no-cpu-throttling --cpu-boost`. O ensaio local CPU observou RSS de 7,76 GiB e
aquecimento de 23,5 s, antes da bateria. O consumo total inclui também o Cass;
esses 8 GiB precisam ser validados com os dois processos e requisições reais.
A variante leve (seção 5) usa `--memory=2Gi --cpu=1 --cpu-boost`.
Este passo não publicou serviços nem contratou infraestrutura. Consulte os resultados e limitações em
[`avaliacao-decider.md`](avaliacao-decider.md).

### Construir a variante no Cloud Build

O `--source .` anterior usa o argumento padrão `false` do Dockerfile. Para
construir a variante `true` sem Docker local, use uma configuração explícita do
Cloud Build. Crie o repositório uma vez, com uma conta autorizada a criá-lo, e
garanta que a conta de build tenha permissão de escrita nele:

```bash
REPOSITORIO="cass-imagens"
TAG="decider-$(date +%Y%m%d-%H%M%S)"
IMAGEM="${REGIAO}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORIO}/agent-cass:${TAG}"

gcloud artifacts repositories create "$REPOSITORIO" \
  --project "$PROJECT_ID" --location "$REGIAO" --repository-format=docker

cat > /tmp/cloudbuild-cass-decider.yaml <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: ['build', '--build-arg', 'INCLUIR_DECIDER=true', '-t', '${IMAGEM}', '.']
images: ['${IMAGEM}']
timeout: 1800s
YAML

gcloud builds submit . --project "$PROJECT_ID" --region "$REGIAO" \
  --config=/tmp/cloudbuild-cass-decider.yaml
```

A construção não promove a imagem. Depois de validar a imagem e repetir a
bateria, ela pode ser publicada com o Decider ainda desligado:

```bash
gcloud run deploy "$SERVICO" \
  --project "$PROJECT_ID" --region "$REGIAO" \
  --image "$IMAGEM" --service-account "$CONTA" \
  --memory=8Gi --cpu=4 --no-cpu-throttling --cpu-boost \
  --concurrency=1 --max-instances=2 --timeout=60 \
  --allow-unauthenticated \
  --update-secrets=METEOMATICS_USER=meteomatics-user:latest,METEOMATICS_PASSWORD=meteomatics-password:latest \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION},GEMINI_MODEL=${GEMINI_MODEL},APP_ORIGIN=${APP_ORIGIN},DADOS_BASE=${DADOS_BASE},DECIDER_ATIVO=false,DECIDER_URL=http://127.0.0.1:8099/v1/systemone"
```

**Critério de ativação:** a bateria com Decider deve ficar igual ou melhor que
a variante desligada e a latência deve caber nos prazos do Cass (1,5 s por consulta
ao Decider e 35 s para a pergunta completa). Enquanto isso não for demonstrado,
mantenha `DECIDER_ATIVO=false` em produção. O piloto atual não satisfaz esse
critério. Só depois da validação, altere a variável para `true` numa nova revisão,
guardando a revisão anterior conforme o procedimento de rollback acima.
[Referência dos parâmetros do Cloud Run](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy),
[build de containers](https://docs.cloud.google.com/build/docs/building/build-containers).

O host desta avaliação não tem Docker: foram verificados dependências CPU,
checkpoints e servidor reais em ambiente Python separado, mas os comandos de
build e execução da imagem acima ainda precisam ser executados num host com
Docker. A bateria completa também precisa ser repetida após a correção do
avaliador registrada no relatório.
