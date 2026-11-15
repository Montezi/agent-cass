# Onde e quando o Cass consulta os dados

Inspeção em 15/11/2026 do frontend local
`/home/mirian-lima/Documents/pessoal/Hackathon Nasa 2026/projects/cassaandra-alert-frontend`.
O frontend foi consultado somente para leitura. Os contratos deste backend não
foram substituídos pelos documentos mais recentes encontrados nele.

## Fluxo implementado no frontend

| Ação | Consulta | Implementação no frontend |
|---|---|---|
| Abrir o mapa | `/dados/hotspots.geojson` | `src/features/mapa/useHotspots.ts:13` |
| Mostrar o indicador ENSO | `/dados/enso.json` | `src/app/EnsoBadge.tsx:12` |
| Abrir um hotspot | `/dados/stats/<id>.json` | `src/features/painel/PanelContent.tsx:36` |
| Abrir a previsão do hotspot | `/api/clima?hotspot=<id>&lat=<lat>&lon=<lon>` | `src/features/painel/useForecast.ts:21` |
| Falha da previsão, inclusive timeout de 8 s | `/dados/forecast-snapshot.json` | `src/features/painel/useForecast.ts:36` |
| Enviar uma pergunta ou sugestão ao Cass | `POST /api/agente`, prazo de 45 s no app e 35 s no Cass | `src/features/cass/agentApi.ts:46` |
| Inicializar o mapa ou trocar o tema | OpenFreeMap, cartografia de fundo | `src/features/mapa/basemap.ts:10` |

Os JSON e as imagens de `/dados` são arquivos publicados no Firebase Hosting.
O cliente acrescenta `?v=<commit>`, compartilha o carregamento durante a página
e não faz polling. O Hosting usa cache de um ano com `immutable`; atualizar os
dados exige uma publicação com nova versão. Ver `src/shared/lib/data.ts:8`,
`src/shared/lib/useData.ts:10` e `firebase.json:21` do frontend.

O frontend atual ainda não consome SSE. Seu rewrite já aceita os descendentes
de `/api/agente`, incluindo `/api/agente/stream`. Em produção, `firebase.json:7`
encaminha esse caminho para o Cloud Run **agente**, e `/api/clima` para **clima**,
ambos em `us-central1`. Em desenvolvimento, `vite.config.ts:30` encaminha `/api`
para `API_PROXY_TARGET`, com padrão `http://localhost:8080`.

O chat tem uma demonstração local que monta respostas a partir dos arquivos.
Alguns erros HTTP, JSON ausente ou resposta incompatível ativam esse modo
visível; as próximas perguntas permanecem nele até remontar o chat. Isso não
comprova que o backend esteja funcionando (`CassChat.tsx:54`, `agentApi.ts:34`).

## Fluxo implementado neste backend

O Cass obtém `DADOS_BASE/hotspots.geojson`, com prazo de 5 s e cache local de
300 s. O prefetch e as ferramentas consultam esses dados quando precisam
identificar ou explicar um hotspot. A previsão (`previsao_tempo`) chama direto a
mesma função da `GET /api/clima` deste serviço, sem HTTP e com o mesmo cache. Ver
[`cass/ferramentas.py`](../cass/ferramentas.py).

### `GET /api/clima?hotspot=<id>&lat=<lat>&lon=<lon>`

Segue a seção `/api/clima` do `api-contract.md` e o `forecastResponseSchema` do
frontend: `source`, `updated_at` (UTC, com `Z`) e `days` com até 7 itens de `date`
e `precipitation_mm`.

- **Pedido:** `hotspot` precisa estar no `hotspots.geojson` (senão 422, com os ids
  válidos em `ctx.validos`). `lat` e `lon` precisam ser as do hotspot (senão 422):
  as fontes só são consultadas nas coordenadas do `hotspots.geojson`, para a rota
  não virar um proxy da conta da Meteomatics. Sem o `hotspots.geojson`, 503.
- **Cadeia**, com prazo de 3 s por fonte ao vivo e 5 s para o snapshot:
  1. **Meteomatics**, só se `METEOMATICS_USER` e `METEOMATICS_PASSWORD` existirem.
     `precip_24h:mm` às 00:00Z de D+1 é a chuva do dia D (UTC); `updated_at` é o
     `dateGenerated` da resposta.
  2. **Open-Meteo** (sem chave): `daily=precipitation_sum`, `timezone=GMT`. A série
     para no primeiro dia sem valor; `updated_at` é a hora da consulta, porque a
     resposta não diz quando o modelo rodou.
  3. **`{DADOS_BASE}/forecast-snapshot.json`**: os dias do hotspot e o `updated_at`
     do arquivo.

  Uma fonte que falha (timeout, HTTP, conexão, redirecionamento) ou sai do formato
  (chuva negativa ou nula no primeiro dia, outra unidade) passa a vez para a
  próxima. Se as três falharem, 503: o app lê o snapshot por conta própria.
- **Cache** por hotspot: 30 min quando serviu a Meteomatics ou a Open-Meteo, 60 s
  quando serviu o snapshot (assim, um minuto depois, as fontes ao vivo são tentadas
  de novo). Uma falha total não fica em cache. Com as duas fontes ao vivo sem
  responder, chegar ao snapshot leva cerca de 6 s, dentro dos 8 s do app.
- **Aquecimento** (desligado por padrão): com `AQUECER_PREVISOES=true`, na subida o
  serviço busca a previsão dos hotspots em paralelo, em segundo plano; o `/health`
  não espera. Com `--cpu-throttling` no Cloud Run, esse trabalho fica sem CPU, por
  isso o padrão é `false` (docs/deploy.md, seção 5). Um pedido que chega enquanto o
  mesmo hotspot está sendo buscado espera esse resultado em vez de consultar de novo.

As ferramentas `explicar_classe`, `hipotese` e `exposicao` ainda devolvem
`SEM_STATS`: o backend não lê `stats/<id>.json`. Esse é um trabalho de integração
pendente; o frontend já tem arquivos e contrato para eles. As URLs do backend
também não recebem a versão do commit, o que precisa ser conciliado com o cache
do Hosting ao publicar dados novos.

## O que ainda não está integrado

Os arquivos presentes no frontend têm marcação de **dados de exemplo**. O
navegador não consulta diretamente NISAR/ASF, NOAA, Sentinel, FIRMS, JRC ou
WorldPop. Essas fontes aparecem nas atribuições e no desenho do pipeline; sua
ingestão científica não foi encontrada nesse repositório.

No Cass, quando a previsão vem do snapshot, `previsao_tempo` devolve a fonte
"Snapshot" com a data do arquivo, `reserva: true`, `motivo_da_reserva` (as falhas
da Meteomatics e da Open-Meteo) e `dados_de_exemplo`. Se as três fontes falharem,
devolve um erro com os três motivos.

Há diferenças entre as cópias dos contratos: o frontend documenta dados v2
(`baseline_pass`, `threshold_db`, `metadata.schema_version`, `generated_at`) e
erros 400 para pedidos inválidos; este backend ainda valida com 422. O limite da
pergunta (até 500 caracteres), a restrição do `hotspot_id` aos ids do
`hotspots.geojson`, o corpo de até 2 KB (recusado com 413) e o `days` de 1 a 16
itens na `/api/clima` já seguem o frontend.
A integração deve reconciliar
o resto em um passo próprio, preservando o contrato público durante esta tarefa.

## Relação com a avaliação do Decider

As três decisões usam mensagens do usuário: se a pergunta está no escopo, tenta
alterar instruções ou identifica um hotspot com clareza. Seus exemplos rotulados
não são respostas das APIs científicas. Podemos fazer um piloto com perguntas
sintéticas, sem dados científicos reais; uma avaliação completa exige exemplos
representativos e rótulos auditados. O estado e os resultados desse piloto estão
em [`avaliacao-decider.md`](avaliacao-decider.md).
