# Diagnóstico do reconhecimento e seleção das próximas melhorias

Data: 14/10/2026. Código analisado: `b5e30916491595fa8d4ca67ce44b459a62178437`.

Este é um diagnóstico histórico, anterior às correções de família tipográfica e
divisória. O baseline foi preservado em
[`iteration-02-before-realism.json`](iterations/iteration-02-before-realism.json).
Os resultados da implementação posterior estão no [resumo atual](README.md).

A recomendação é melhorar a descoberta por família tipográfica e a geometria dos números, preservando a rejeição de leituras incertas. Trocar o OCR ou reduzir limites de aceitação, isoladamente, não resolve o principal gargalo e pode produzir valores errados aparentemente confiáveis.

## Evidência nova desta análise

Reexecutei, com instrumentação e sem modificar o leitor de produção, os **193 casos que tinham algum valor elegível desconhecido** no modo adaptativo com OCR. Os resultados reproduziram os do benchmark anterior, sem diferenças nos valores/qualidades. Os hashes da implementação e as versões das dependências correspondem ao benchmark original. A reexecução é diagnóstica; não constitui uma nova rodada completa das três técnicas nem uma nova estimativa independente de generalização.

O benchmark de referência continua sendo **979/1.491 leituras exatas, 512 desconhecidas e zero incorretas aceitas**. A classificação abaixo localiza o ponto em que cada uma dessas 512 leituras foi perdida:

| Estágio de rejeição | Campos | Participação nas perdas |
|---|---:|---:|
| Não formou conjunto suficiente de rótulos: 83 imagens × 4 campos | 332 | 64,8% |
| Painel encontrado, mas rótulo individual ausente | 36 | 7,0% |
| Rótulos encontrados, mas coluna não validada: 5 imagens × 4 campos | 20 | 3,9% |
| Rótulo presente, mas região numérica individual rejeitada | 26 | 5,1% |
| Região disponível, mas leitura numérica rejeitada | 98 | 19,1% |
| **Total** | **512** | **100%** |

Agrupando: **368 perdas nas âncoras, 46 na geometria e 98 no leitor numérico**. O estágio de rejeição não prova que todos os campos se tornariam legíveis ao corrigir apenas esse estágio: problemas numéricos podem estar escondidos atrás de um painel ainda não detectado.

Os 98 erros de leitura se dividem em: 51 correspondências fracas de dígitos, 33 separadores de milhar fracos, 8 dígitos ambíguos, 3 formas instáveis ao variar o limiar, 2 candidatos com zero à esquerda e 1 topologia não suportada. Nos 51 casos de dígito fraco, **48 candidatos principais estavam numericamente errados**. Nos 33 casos de separador fraco, 25 candidatos tinham o valor correto, mas 8 não. Não existe justificativa para simplesmente aceitar esses candidatos.

## Por que as falhas acontecem

### 1. Escala e família tipográfica são misturadas

Em `tibia_client/adaptive.py`, `_anchors` pesquisa uma faixa relativamente densa, mas `_native_anchors` e `_alternate_anchors` usam apenas seis fatores e redimensionamento de máscaras binárias por vizinho mais próximo. `_refine_anchors` usa RGB, fases subpixel e pequenas variações de aspecto, porém somente para os templates da família principal.

Redimensionar um texto antialiasado e depois binarizá-lo não produz a mesma forma que redimensionar uma máscara já binarizada. O problema é especialmente forte em letras de poucos pixels. Um ponto de amostragem diferente pode apagar uma haste, fechar um espaço ou fundir caracteres.

Em reduções fortes, parte da informação pode ter sido perdida de forma irreversível. Ampliar a imagem não restaura automaticamente os traços originais. O benchmark deve distinguir falha corrigível do leitor de uma captura que já não permite decidir o dígito com segurança; nesses casos, a solução é obter captura nativa melhor e continuar retornando desconhecido.

Há também uma restrição concreta em `adaptive_text.py`: os dígitos alternativos Yonabra e suas vírgulas só são considerados perto da escala 0,7. Ao ampliar a mesma imagem, a estimativa de escala muda e esses assets deixam de participar. A família tipográfica continua sendo a mesma, mas o código não preserva essa identidade no caminho numérico.

A cobertura agregada por fonte reforça a investigação: Yonabra tem 27/115 valores exatos, enquanto a referência Paulistinha tem 102/115. Isso não isola causalmente o efeito da fonte; o código e as sondagens explicam mecanismos específicos a corrigir.

### 2. A geometria confunde divisória com coluna numérica

Em `_layout`, a exigência de coluna única é aplicada **antes** da detecção da divisória do painel. No caso `dfg-retro-pvp-rp535::panel-scale-2`, uma linha vertical de um pixel passa pelos filtros de três linhas e cria uma segunda coluna.

Forçar refinamento e OCR não resolveu o caso. Em uma cópia temporária, remover apenas essa divisória permitiu ao leitor atual reconhecer exatamente os quatro valores: **5455, 7995, 100 e 4250**. Essa intervenção demonstra a causa; não é um algoritmo pronto nem autoriza ignorar qualquer linha vertical.

### 3. Segmentação separa um número completo em blocos

Na referência Paulistinha redimensionada a 75%, o `710` é separado em dois blocos pelo limiar e pela regra de espaçamento. A geometria encontra a coluna, mas descarta Health para impedir que apenas o sufixo seja interpretado como número completo. A rejeição é apropriada; o agrupamento dos caracteres precisa melhorar.

A leitura também depende de projeções verticais, altura relativa para reconhecer vírgulas e contagem rígida de buracos internos. O fallback de dígitos unidos só é acionado quando `allow_grouping=False`, portanto não atende Health, Mana e Capacity. A lista de alternativas atual muda um dígito por vez, limitando a correção de erros de segmentação ou de múltiplos caracteres.

### 4. O OCR atual não é um leitor numérico geral

Ele só pode resolver um template forte, porém ambíguo. Falhas de dígito fraco, separador, topologia ou segmentação não chegam a uma aceitação independente pelo OCR. Essa restrição evita aceitar números de fontes ou formas não sustentadas pelos pixels; removê-la exige uma nova política de evidência e validação.

### 5. Recuperação ainda custa segundos

O relatório de runtime preservado mede mediana de aproximadamente **6,1 s** para recuperar um painel movido e máximo de **9,7 s**. As leituras com layout já resolvido são rápidas: mediana **7,59 ms**, p95 **21,57 ms**. Melhorar apenas a acurácia estática não elimina períodos longos sem leitura após mudanças de interface.

## Experimentos exploratórios desta rodada

| Experimento | Resultado observado | Decisão |
|---|---|---|
| Forçar refinamento/OCR no caso da divisória | Continua sem layout | O problema é geométrico, não falta de reconhecimento dos rótulos |
| Remover a divisória apenas na cópia de diagnóstico | Recupera quatro valores exatos | Priorizar detecção de chrome antes da seleção de coluna |
| Ampliar 3×, inverter e acrescentar borda no OCR numérico de 8 campos ambíguos; preservar as regras atuais de aceitação | Variante em tons de cinza recupera 0; binária recupera 1; nenhuma aceitação errada nesses oito | Melhoria complementar, com pouco ganho demonstrado neste grupo |
| Aplicar ampliação 3×, inversão em cinza e borda ao OCR de rótulos em 73 recortes de painel anteriormente não encontrados; manter templates e leitor numérico | Encontra 40 painéis; 59 valores exatos, **2 incorretos aceitos**, 231 desconhecidos entre 292 campos | **Não aprovar essa variante para produção**; investigar conjuntamente descoberta e evidência numérica |

Os dois erros novos foram **185 → 105** e **1451 → 1151**. A busca mais abrangente expôs casos que antes terminavam em `UNKNOWN` na descoberta. Os mesmos filtros numéricos não foram suficientes nas novas condições. Por isso uma melhoria na descoberta não pode ser avaliada somente pela quantidade de painéis encontrados.

Essas sondagens foram selecionadas a partir de falhas conhecidas. Não medem ganho final no benchmark completo, não possuem validação nova independente e não comprovam segurança fora do subconjunto examinado.

## Técnicas consideradas e seleção

| Técnica | Potencial e custo | Decisão |
|---|---|---|
| Motivos estruturados por estágio e por campo, candidatos e margens | Custo baixo; torna cada próxima mudança mensurável | Implementar primeiro |
| Detectar bordas/divisórias antes das colunas; testar geometria por candidato | Custo baixo/médio; causa concreta demonstrada | Primeira correção funcional |
| Modelo explícito de família tipográfica, altura renderizada, escala horizontal/vertical e evidência | Custo médio; unifica reconhecimento de rótulos, dígitos e vírgulas | Prioridade central |
| Busca grosseira seguida de refinamento RGB/subpixel local para todas as famílias | Custo médio; ataca o maior grupo de perdas; exige limite de custo | Implementar junto ao modelo de renderização |
| Projetar a localização de uma linha ausente a partir das outras três | Bom para restringir a busca; perigoso se virar identidade presumida | Usar somente para propor a região; exigir confirmação visual antes de aceitar o recurso |
| Segmentação por sequência com hipóteses limitadas de separar/unir caracteres | Custo médio/alto; aborda sufixos, dígitos unidos e vírgulas | Próxima etapa, com margem entre strings completas |
| Comparação em tons de cinza/gradientes ou distância a contornos, além das máscaras | Pode tolerar deslocamento e antialiasing; requer medir falsos positivos | Avaliar como evidência complementar à forma e à geometria |
| Pré-processamentos limitados e OCR especializado por linha | Custo moderado de latência; ganho local pequeno, ganho na descoberta inseguro sem mais validação | Manter como candidato experimental, fora do loop sensível à latência |
| Segundo motor OCR ou pequeno reconhecedor de sequência treinado | Pode ampliar fontes e segmentação; aumenta dependências e necessidade de dados | Adiar até medir o resíduo após correções estruturais |
| Consistência temporal | Útil contra eventos transitórios; não resolve erro sistemático da mesma fonte | Usar por recurso e com prazo; não atrasar queda real de Health para esperar valores iguais |
| Super-resolução generativa | Pode inventar traços que mudam o dígito | Não usar como evidência para aceitar números |
| Reduzir limiares ou aproveitar o último valor conhecido como leitura atual | Melhora cobertura aparente, compromete identidade/frescor | Rejeitar |

Para a família de fontes, o layout deve transportar a hipótese de renderização para o leitor: família, altura observada, escalas e evidências. Não deve selecionar assets por nome do screenshot nem pela resposta esperada. A altura numérica também deve ser verificada localmente, sem presumir que todos os rótulos e dígitos têm a mesma caixa vertical.

Para a sequência numérica, a busca deve explicar o **número inteiro**, com bordas visíveis, posições de separadores, largura dos caracteres e margem contra a segunda melhor string. Expandir uma ROI não pode englobar outra linha; unir componentes não pode absorver a divisória. O limite de dígitos configurado por recurso deve entrar durante a inferência, não apenas filtrar o resultado depois.

Contagem de buracos pode permanecer como sinal de forma, mas um resultado obtido com um único limiar não deve ser a única justificativa para eliminar um dígito concorrente. Concordância entre variantes de pré-processamento também não é independência estatística: elas podem compartilhar o mesmo erro.

## Ordem de implementação e critérios

1. **Instrumentação e correção de geometria.** Persistir motivos e evidências no benchmark; corrigir divisória/coluna e adicionar os casos mínimos derivados das falhas. Preservar rejeição de prefixos/sufixos, números de outras linhas e painéis duplicados.
2. **Família e renderização, em conjunto para rótulos e números.** Aplicar bancos e refinamento a todas as fontes, incluindo vírgulas. Verificar os dois novos falsos positivos antes de ampliar o caminho aceito. Uma descoberta nova pode continuar `UNKNOWN` até haver evidência numérica suficiente.
3. **Segmentação completa do número.** Resolver blocos separados e caracteres unidos com hipóteses limitadas; avaliar OCR/preprocessamento como confirmação, sem relaxamento global de confiança.
4. **Recuperação e orçamento de latência.** Reutilizar família/escala já confirmadas, buscar primeiro perto da última localização e escalar a busca quando necessário. Manter OCR e descoberta em background e invalidar imediatamente dados/ações da revisão antiga.

Para cada etapa: comparar o mesmo conjunto congelado, executar a matriz completa de imagens e os testes afetados; mostrar ganho/perda por fonte, família de transformação e estágio. Zero novos valores/associações incorretos e zero regressões nos casos intencionalmente desconhecidos são condições necessárias, mas não provam ausência de erro em produção. Uma variante que melhora cobertura e introduz falso positivo não é aprovada. O candidato de pré-processamento de rótulos desta análise é um exemplo real dessa reprovação.

Não há base para prometer uma porcentagem final antes dos testes. O maior grupo de perdas determina onde pesquisar, não quantos valores a correção necessariamente recuperará.

## Como tornar o benchmark mais confiável

O agregado atual é dominado por **1.092/1.491 campos (73,2%)** oriundos de 273 recortes de painel redimensionados. Eles são úteis como teste de estresse, mas têm menos distratores que telas completas e compartilham somente 13 fontes positivas. Não equivalem a 273 capturas independentes.

Separar os seguintes resultados:

- descoberta em tela completa;
- leitura numérica condicionada a uma região corretamente encontrada;
- identidade e associação à linha;
- cobertura exata e erros aceitos por fonte, família, escala e transformação;
- inicialização, recuperação e leitura estável, com p50, p95 e máximo;
- target confirmado, clientes visualmente semelhantes e recortes legados.

É necessário ampliar a evidência do cliente alvo: atualmente há somente uma imagem confirmada da família alvo, no conjunto de treino, e um único negativo real. Capturas nativas em diferentes escalas/DPI, larguras e layouts precisam ser separadas de imagens redimensionadas depois da captura. Adicionar negativos difíceis: chat com nomes dos recursos, tabelas de números, painéis parcialmente cobertos, limites cortados, duas janelas/painéis e divisórias.

Preservar o agrupamento por fonte ao separar desenvolvimento e validação. Como esta análise inspecionou falhas do conjunto anteriormente chamado heldout, novos ajustes guiados por essas falhas devem tratar esses casos como regressões conhecidas; a próxima comprovação de generalização precisa de capturas ainda não usadas nas decisões. Também executar ablações: OCR apenas nos rótulos, apenas nos números e nos dois; família tipográfica, refinamento e segmentação ligados separadamente.

A melhor métrica de seleção é cobertura correta sob um limite explícito de erro aceito e latência, acompanhada de limites por categoria. Não usar média agregada para esconder regressão do cliente alvo, falhas de Health ou recuperação lenta.

## Fontes técnicas e reprodução

Documentação primária consultada: [Tesseract — Improving the quality of the output](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html), [OpenCV — Image thresholding](https://docs.opencv.org/4.x/d7/d4d/tutorial_py_thresholding.html). Elas fundamentam os candidatos de pré-processamento; os ganhos e falhas relatados acima vêm dos experimentos locais.

Evidências locais:

- `diagnosis/summary.json`: contagens, correspondência com o baseline, ambiente e hash do relatório original.
- `diagnosis/failure-traces.jsonl`: os 193 casos instrumentados.
- `diagnosis/geometry-probes.jsonl`: componentes e colunas nos exemplos geométricos.
- `diagnosis/column-divider-probe.json` e `diagnosis/divider-reproduction.json`: intervenção controlada na divisória, reproduzida independentemente.
- `diagnosis/ocr-preprocessing-probe.json`: teste numérico com oito campos.
- `diagnosis/label-preprocessing-probe.jsonl`: teste de descoberta nos 73 casos.
- `diagnosis/new-false-accepts.json`: evidência dos dois novos erros.
- Scripts em `diagnosis/`; para reproduzir os números históricos, criar um
  worktree separado no commit `b5e30916491595fa8d4ca67ce44b459a62178437`, copiar
  `reports/diagnosis/` para ele e executar a partir dessa raiz, com as dependências
  fixadas e Tesseract 5.3.4. Nesse checkout, `reports/resource-benchmark.json` é o
  baseline original. Executar os probes no leitor atual produz um experimento
  diferente. Eles sobrescrevem somente seus relatórios diagnósticos.

O código de produção, os screenshots originais e o benchmark de referência não foram modificados nesta avaliação. Os 772 testes aprovados pertencem à rodada anterior; nesta rodada foram executados diagnósticos e sondagens, não uma nova suíte completa. Nenhum resultado exploratório substituiu o score oficial de 65,7%.
