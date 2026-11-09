# Base própria de CNPJ e modo só dados abertos

## Origem dos dados (conferido em 09/11/2026)
- Página oficial: gov.br/receitafederal, "Dados públicos CNPJ"; layout em `cnpj-metadados.pdf`.
- Arquivos: compartilhamento público do SERPRO `https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9`,
  por pasta `AAAA-MM` (o antigo `dadosabertos.rfb.gov.br` não responde mais).
- Município: código da Receita (TOM, 4 dígitos), não o IBGE. O importador usa o `Municipios.zip` da competência.

## Rotina mensal (nada agendado; a rodada é manual)
A Receita publica uma pasta nova por mês. Rodar depois que `AAAA-MM` aparecer:

```bash
cd workers
python -m radar.importar_cnpj --uf PI                      # um estado, última competência
python -m radar.importar_cnpj                              # Brasil inteiro
python -m radar.importar_cnpj --competencia 2026-10 --dir /caminho/dos/zips
```

- Sempre lê os 20 arquivos grandes (cerca de 7 GB compactados), mesmo com `--uf`: a Receita não separa por estado.
- Para o Brasil inteiro, baixe antes com `curl -C -` e use `--dir`, para não perder a importação numa queda de rede.
- Primeira carga do Brasil inteiro: use `--recriar-indice` (exige o dono da tabela; bloqueia a tabela até o fim).
- Idempotente: linha igual não é regravada, CNPJ que deixou de estar ativo sai. Uma falha desfaz tudo.
- Trava: se a carga vier com menos da metade da base atual, aborta (`--forcar` passa por cima).

## Variáveis
| Variável | Padrão | Efeito |
|---|---|---|
| `RADAR_FONTE_CNPJ` | `base_local` | `base_local` (custo zero) ou `casadosdados` (paga) |
| `RADAR_INSTAGRAM_PAGO` | `1` | `0` desliga busca e leitura de perfil pagas; o @ pelo site continua |
| `RADAR_SO_DADOS_ABERTOS` | `0` | `1` garante custo zero: sem Apify, Casa dos Dados, IA e WhatsApp |

No modo só dados abertos, qualquer chamada paga falha (`FontePagaBloqueada`) antes de sair da máquina, e a falha é definitiva.
A `RADAR_CONTATO` é ignorada. A UF ainda não importada deixa o CNPJ como `nao_verificado`.

## Rodada de teste sem deploy (só leitura)
`python -m radar.rodada_dados_abertos` busca no gosom, procura o CNPJ na base local e lê os sites. Não usa a fila
(o worker de produção pegaria o job com as fontes pagas) e a conexão é aberta em modo somente leitura.
O arquivo de saída mostra o que seria gravado em `radar.leads`, `radar.empresas`, `radar.presenca` e `radar.custos_api`.
