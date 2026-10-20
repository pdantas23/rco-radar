# Teste gosom/google-maps-scraper v1.18.1 (2026-10-20, VPS principal)
Container `radar-gosom-teste`, --cpus=1 --memory=1.5g, porta só em 127.0.0.1:18080. Keyword "barbearia", pt, zoom 15, Teresina centro (-5.0892,-42.8016), email=false.

| Job | depth | Tempo | Lugares | Pico CPU | Pico memória | Carga VPS máx |
|---|---|---|---|---|---|---|
| d1 | 1 | 46 s | 16 | 114% | 910 MiB | 4,65 |
| d2 | 2 | 56 s | 20 | 108% | 967 MiB | 3,88 |
| d3 | 3 | 73 s | 28 | 108% | 800 MiB | 3,01 |

- Bug #338 (depth>=3 "target closed") NÃO ocorreu na v1.18.1 aqui: depth 3 terminou ok, 28 lugares, 0 jobs falhos.
- Sem captcha/bloqueio do Google nos logs. Só 3 jobs (limite era 4).
- Campos (CSV): título, categoria, endereço + complete_address (rua, bairro, cidade, CEP, estado), telefone (16/16, 19/20, 25/28), website (9/16, 11/20, 13/28), nota e nº de avaliações (100%), distribuição por estrelas, horário por dia (100%), place_id, cid, data_id, lat/lng, plus_code, timezone, proprietário (quando reivindicado), avaliações com texto (user_reviews, 100%), imagens, link. Coluna `status` (aberto/fechado) veio vazia nos 3 jobs; `emails` vazio (email=false).
- Itens ~1 lugar duplicados: nenhum (place_id únicos).

# 2o teste (2026-10-20 02:05-02:17 UTC): modo mais rápido
Mesmo ponto/keyword do 1o teste, 3 buscas autorizadas. Arquivos com prefixo `t2_`. Container recriado (o do 1o teste já não existia), mesmas travas; na busca c) `--cpus=2 --memory=3g --memory-swap=3g -c 2`.

| Config | Tempo | Lugares (únicos) | Lugares/min | Pico CPU | Pico mem | Carga VPS máx |
|---|---|---|---|---|---|---|
| 1o teste d1 (1 núcleo) | 46 s | 16 | 20,9 | 114% | 910 MiB | 4,65 |
| 1o teste d2 | 56 s | 20 | 21,4 | 108% | 967 MiB | 3,88 |
| 1o teste d3 | 73 s | 28 | 23,0 | 108% | 800 MiB | 3,01 |
| d6 (1 núcleo) | 128 s | 48 | 22,5 | ~114% | 1009 MiB | 4,29 |
| d10 (1 núcleo) | 219 s | 76 | 20,8 | ~161%* | 1017 MiB | 4,64 |
| c) 2 jobs d3 "salão de beleza", 2 pontos, `-c 2` / 2 núcleos | 93 s total | 56 (54 únicos) | 36,1 (34,8 únicos) | ~221% (pico isolado 443%*) | 1,32 GiB de 3 GiB | 6,38 |

*leituras do `docker stats` acima da trava são artefato de amostragem (janela curta); média sustentada fica no limite (100% / ~210%).

- Ritmo por núcleo é ~21-23 lugares/min em qualquer profundidade: aumentar depth só aumenta o volume por busca (d10 = 76 lugares num ponto, d6 = 48), não a velocidade. Telefone 84-100%, site 35-54%.
- Paralelismo: `-c 2` + 2 núcleos deu ~1,6x (35 vs 22 lugares/min). Obs.: no modo web (API) os jobs são executados em FILA (o 2o ficou "pending" até o 1o terminar, 48 s); o `-c` paraleliza dentro de cada job, não entre jobs. Os 2 pontos rodaram um após o outro, cada um com 2 workers.
- Bloqueio do Google: nenhum sinal (0 captcha, 0 jobs falhos, 0 resultados vazios) em 6 buscas + ~200 páginas de lugar no total. Volume ainda pequeno para concluir sobre limite de taxa.
- Produção: crm_crm-rco, crm_saas-rco, evolution-go (+db, pgbouncer) 1/1 sem mudança; 0 erros nos logs após cada rodada; carga da VPS voltou a ~2-4 em 1-2 min (pico 6,38 em 4 núcleos, só com a config de 2 núcleos).
- Duplicados entre pontos: 2 de 56 (pontos a ~4 km se sobrepõem pouco).
