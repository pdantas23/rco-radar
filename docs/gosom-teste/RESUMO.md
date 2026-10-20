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
