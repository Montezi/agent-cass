---
name: medido-vs-hipotese
description: Separar o que o radar mediu da hipótese sobre a mudança. Ative quando a resposta tiver números do radar ou o usuário perguntar por que algo mudou.
---
# Medido × Hipótese

## Medido

- É só o que as ferramentas devolveram como medição: classe dominante, área
  afetada (km²), confiança e data da passagem do radar.
- Cada frase de medido tem número, unidade, data e fonte, com o número entre `**`:
  "**212 km²** de água nova, passagem do radar de 12 nov 2026 (NISAR)."
- Valor `null` não foi medido: diga "ainda não medido". Nunca estime, arredonde ou
  complete um número que a ferramenta não deu.
- As frases vão no campo `medido`; o `texto` resume sem repeti-las.

## Hipótese

- Só existe hipótese se alguma ferramenta devolveu dados de hipótese nesta
  conversa (um campo `hipotese` com motor físico, evidência e força).
- Sem esses dados, não ofereça hipótese nenhuma: deixe `hipotese` vazio e diga que
  a hipótese ainda não foi calculada pelo pipeline. Não monte hipótese a partir do
  `enso_effect`, da sua memória ou do bom senso.
- Com dados: diga o motor físico provável, a evidência e a força (fraca, moderada
  ou forte), do jeito que a ferramenta devolveu.
- Hipótese não é causa comprovada: "pode estar ligada a", nunca "foi causada por".

## Nunca

- Afirmar que o El Niño causou a mudança medida.
- Pôr número medido e hipótese na mesma frase.
