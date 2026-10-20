# Subir os robôs e o gosom na VPS

Decisão (20/10/2026): serviço do tipo **Compose** no EasyPanel, lendo `docker-compose.yml` da raiz do repo
(robôs + gosom na rede do banco interno, sem porta publicada). O texto abaixo é o plano original.


Precisa de aprovação do Philip antes de cada passo (mudança de infra em produção).

## 1. gosom (Google Maps, grátis)
Configuração medida em 20/10/2026 (docs/gosom-teste/RESUMO.md): 2 núcleos, ~35 lugares/min, carga máx. 6,4.

```
docker run -d --name radar-gosom --restart unless-stopped \
  --cpus=2 --memory=3g --memory-swap=3g \
  -v radar_gosom_data:/gmapsdata --network <rede interna dos robôs> \
  gosom/google-maps-scraper:v1.18.1 -web -data-folder /gmapsdata -c 2
```
Sem porta publicada: só os robôs acessam (GOSOM_URL=http://radar-gosom:8080).
Ideal: serviço no EasyPanel (projeto `servicos`) para não sumir em limpeza do painel.

## 2. Robôs (workers/Dockerfile)
- 1 container no começo; escala com réplicas (a fila usa SKIP LOCKED).
- Acesso ao banco: conexão direta ao Postgres da instância `supabase` pela rede interna.
  Proposta: papel próprio `radar_worker` (migration futura, aprovar o SQL), com acesso só ao
  schema `radar` + leitura das tabelas do COMERCIAL que o Radar usa — em vez da senha do `postgres`.
- Variáveis: RADAR_DATABASE_URL, GOSOM_URL, GOSOM_DEPTH=10, APIFY_TOKEN, CASADOSDADOS_API_KEY.
  Fase 3: ENCRYPTION_KEY (a mesma do COMERCIAL) para ler a chave de IA da agência.

## 3. Antes do primeiro lote real
- Ligar o Radar na agência RCO HUB e definir o teto mensal (`radar.agencias`).
- Primeira pesquisa pequena (~100 lugares), conferir logs e produção, depois escalar.
- Limite diário de buscas no gosom a definir (risco de bloqueio do IP em volume ainda não testado).
