# Registro do piloto do Decider

## 2026-11-15 — preparação local, antes de medir o modelo

- Lida a skill upstream e as referências de avaliação e formato no checkout
  `/tmp/agent-cass-strands-decider`, commit
  `79bf6e7184c166990eec79034667cc0bfd445e12`.
- Escrito conjunto sintético de 36 linhas, em formato upstream, com 12 itens por
  tarefa e PT/EN balanceados. Rótulos ainda sem auditoria independente.
- Congelado o piloto inicial `evaluation/decider-cass.jsonl`, SHA256
  `0ef9a0e67558a7ab6aa7e5e9d76debc1202dc5e95dfe07d02fe4b93d58b68f90`.
- Antes de qualquer medição, revisão de interface encontrou contexto de nome
  candidato que não existe no runtime. O primeiro arquivo foi preservado e uma
  versão separada com estados equivalentes ao runtime foi criada:
  `evaluation/decider-cass-runtime.jsonl`, SHA256
  `72c2003b267f82f83b2297f5848fb024762274aec89cc32da8fa0cef62e7c219`.
- Implementada CLI `scripts/avaliar_decider.py` para medir os mesmos itens com
  padrões `false` e HTTP real `true`, sem mascarar erros como acertos. Inclui
  opção explícita `--gemini` para a bateria completa.
- Validação executada:

  ```bash
  .venv/bin/pytest -q tests/test_avaliacao_decider.py
  ```

  Resultado: **38 testes passaram**, somente com transporte simulado nos testes
  unitários. Isso valida a ferramenta de avaliação; não é um resultado do Decider
  nem da bateria Gemini. Nenhum modelo ou chamada Gemini foi executado nesta
  preparação. Nenhum treinamento/calibração foi feito.

Os comandos e resultados de execução real devem ser acrescentados pelo operador
com caminho do relatório, hashes, checkpoint e recursos usados. Não editar os
JSONL congelados depois de medir; uma nova revisão exige novo arquivo e novo hash.

## 2026-11-15 — primeira execução real, incompleta

- Servido o checkpoint solicitado em CPU com PyTorch `2.7.1+cpu`; revisões,
  saúde e consumo registrados em `results/ambiente-cpu.json`.
- Executada CLI com `--timeout 1.5 --repeticoes 1 --aquecimento 0 --gemini`.
  Relatório preservado em `results/piloto-cpu-2026-11-15.json`; saída 2.
- Parte direta: 1/36 respostas válidas (incorreta) e 35 timeouts. A política de
  fallback acertou 17/36; padrões desligados acertaram 18/36. Isso não permite
  estimar a qualidade do modelo entre as três tarefas.
- Bateria completa: tentativa inválida, com três HTTP 500 por modo causados pelo
  avaliador fechar o event loop do cliente Gemini cacheado. Correção: um único
  lifespan e nova configuração/cota por caso, com teste de regressão.
- Row check posterior com tokenizer local da base e `build_prompt` upstream:
  36 válidas, máximo 95 tokens, janela 4096; nenhum dado/hash alterado.
- Helper de build validado sobre os snapshots reais offline, incluindo base,
  tokenizer, head e LoRA. Docker não está instalado; imagem não construída.
- Após a correção, 56 testes de avaliação/download/entrada do container passaram.
  Suíte completa e repetição com modelos reais ficaram bloqueadas pela revisão
  automática por limite de uso, sem avaliação de segurança concluída.
- Critério do usuário: bateria igual ou melhor e latência aceitável; produção
  mantém `DECIDER_ATIVO=false`. Configuração documentada de deploy: 8 GiB, 4 CPUs,
  sem throttling, com CPU boost. Nenhum recurso foi publicado.
