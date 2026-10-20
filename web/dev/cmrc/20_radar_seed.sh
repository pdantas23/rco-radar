#!/bin/sh
# Aplica 001 + 003 + (adaptação da seed do protótipo com agency_id) no banco local "cmrc".
set -e
RAD=/Users/philip/Desktop/RCOHub/RADAR/rco-radar
PSQL="docker exec -i radar-web-pg psql -U postgres -d cmrc -v ON_ERROR_STOP=1 -q"
$PSQL < $RAD/supabase/migrations/001_radar_schema.sql
$PSQL < $RAD/supabase/migrations/003_escopo_agencia.sql
AG=aaaaaaaa-1111-0000-0000-000000000001
$PSQL <<SQL
do \$\$ declare t text; begin
 for t in select c.relname from pg_class c join pg_namespace s on s.oid=c.relnamespace join pg_attribute a on a.attrelid=c.oid and a.attname='agency_id' where s.nspname='radar' and c.relkind='r' and c.relname not in ('regras_pontuacao') loop
   execute format('alter table radar.%I alter column agency_id set default %L::uuid', t, '$AG');
 end loop; end \$\$;
SQL
# dev nasce VAZIO: só o estrutural (config padrão + regra de pontuação geral). Os dados de exemplo antigos ficam em ../01_seed.sql, sem uso.
$PSQL <<SQL
insert into radar.config (chave, valor) values
 ('custo_unitario_brl','{"maps":0.026,"cnpj":0.01,"instagram":0.011,"anuncios":0.019}'),
 ('limite_diario_custo_brl','50'),
 ('anuncios_ativos','{"ligado": false}');
insert into radar.regras_pontuacao (versao, nicho, pesos, faixas) values
 (1, null, '{"tem_whatsapp":25,"presenca_media_ou_forte":20,"faturamento_30k_ou_mais":20,"google_bem_avaliado":20,"cnpj_ativo":15}', '{"quente":70,"morno":40,"nota_minima":4.0,"min_avaliacoes":50}');
SQL
$PSQL <<SQL
-- regras: a geral vira PADRÃO da RCO (agency_id nulo, já é); a de barbearia é da agência
do \$\$ declare t text; begin
 for t in select c.relname from pg_class c join pg_namespace s on s.oid=c.relnamespace join pg_attribute a on a.attrelid=c.oid and a.attname='agency_id' where s.nspname='radar' and c.relkind='r' loop
   execute format('alter table radar.%I alter column agency_id drop default', t);
 end loop; end \$\$;
insert into radar.agencias (agency_id, ativo, teto_mensal_brl, pipeline_id, stage_id, whatsapp_config_id) values
 ('$AG', true, 500, 'dddddddd-0000-0000-0000-000000000001', (select id from public.pipeline_stages where name='Novo lead'), 'eeeeeeee-0000-0000-0000-000000000001');
grant usage on schema radar to authenticator;
SQL
echo seed ok
