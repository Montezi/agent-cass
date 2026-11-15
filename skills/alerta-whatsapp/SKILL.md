---
name: alerta-whatsapp
description: Montar o rascunho de um alerta para WhatsApp sobre um hotspot. Ative quando o usuário pedir um alerta, um aviso ou uma mensagem para compartilhar.
---
# Alerta para WhatsApp

## Como montar

1. Descubra o hotspot. Se não estiver claro, pergunte e ofereça as opções.
2. Chame `rascunhar_alerta` com o `id` e o idioma da conversa. Ela consulta o
   hotspot e a previsão antes de montar o texto (hipótese e exposição entram quando
   o pipeline as calcular), então os dados vêm antes do rascunho.
3. Ponha o `rascunho` no `texto` como veio. Não troque, arredonde nem acrescente
   números.
4. Diga que é um rascunho: nada é enviado. Quem compartilha é o usuário.

## Ordem do alerta

Medido (com data), hipótese (com força, só se houver dados), quem está na área
(só se houver dados), previsão. Termina com a fonte e o link do app.

## Tom

Calmo, sem alarmismo e sem afirmar causa. Nada de "urgente", "catástrofe" ou
"o El Niño provocou". Se a previsão vier marcada como só referência ou estiver
indisponível, diga isso, sem anunciar chuva.
