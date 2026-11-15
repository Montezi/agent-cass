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
| Enviar uma pergunta ou sugestão ao Cass | `POST /api/agente`, prazo de 20 s | `src/features/cass/agentApi.ts:36` |
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
identificar ou explicar um hotspot. A previsão consulta `CLIMA_URL` com ID,
latitude e longitude, com cache de 600 s. Ver
[`cass/ferramentas.py`](../cass/ferramentas.py).

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

O contrato de clima prevê Meteomatics → Open-Meteo → snapshot, mas a implementação
do serviço Cloud Run `clima` também não está no frontend. O snapshot local é um
fallback implementado; os dois fornecedores ainda precisam ser confirmados no
repositório do serviço. Ver `docs/api-contract.md:60` do frontend.

No Cass, `previsao_tempo` usa a mesma reserva: se a `CLIMA_URL` falhar (timeout,
HTTP, conexão) ou não devolver JSON válido no contrato, lê
`{DADOS_BASE}/forecast-snapshot.json` (formato do `forecastSnapshotSchema`) e
devolve a fonte "Snapshot" com a data do arquivo, `reserva: true` e o motivo. Se
as duas falharem, devolve um erro com os dois motivos.

Há diferenças entre as cópias dos contratos: o frontend documenta dados v2
(`baseline_pass`, `threshold_db`, `metadata.schema_version`, `generated_at`) e
erros 400 para pedidos inválidos; este backend ainda valida com 422. O limite da
pergunta (até 500 caracteres) e a restrição do `hotspot_id` aos ids do
`hotspots.geojson` já seguem o frontend. A integração deve reconciliar
o resto em um passo próprio, preservando o contrato público durante esta tarefa.

## Relação com a avaliação do Decider

As três decisões usam mensagens do usuário: se a pergunta está no escopo, tenta
alterar instruções ou identifica um hotspot com clareza. Seus exemplos rotulados
não são respostas das APIs científicas. Podemos fazer um piloto com perguntas
sintéticas, sem dados científicos reais; uma avaliação completa exige exemplos
representativos e rótulos auditados. O estado e os resultados desse piloto estão
em [`avaliacao-decider.md`](avaliacao-decider.md).
