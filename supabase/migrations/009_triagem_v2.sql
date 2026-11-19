-- RCO Radar — 009: Triagem IA v2 (portões, cadência de 5 toques, esteira, agenda) — contrato: docs/triagem-ia.md, seção v2
--
-- APLICADA na instância `supabase` (vps-rco) em 18/11/2026 com aprovação do Philip, como supabase_admin, depois de ensaio com rollback das cinco (008, 009, 137, 138, 139).
--   docker exec -i supabase_supabase-db-1 psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 < 009_triagem_v2.sql
--
-- O que muda (só radar.conversas; nenhum dado existente é alterado):
-- * decisao aceita as saídas da v2 além das antigas (linhas existentes continuam válidas no CHECK novo);
-- * etapa: em que parte da triagem a IA está (qualificando | oferta de horário | esteira);
-- * cadencia_inicio: hora do toque 1 da cadência em curso (os toques 2-5 contam a partir dela);
-- * esteira_desde / esteira_enviados: entrada na esteira de 30/60/90 dias e quantas mensagens saíram;
-- * agenda: horários oferecidos, SDR, tentativas e o agendamento confirmado ({sdr_id, opcoes, tentativas, escolhido}).
-- Defaults seguros: conversas que já existem ficam etapa 'qualificando' e o resto nulo/zero.

begin;

alter table radar.conversas drop constraint if exists conversas_decisao_check;
alter table radar.conversas add constraint conversas_decisao_check check (decisao in (
  'passar', 'acompanhar', 'descartar',                                                      -- v1 (nota)
  'passa_direto', 'passa_alerta', 'esteira', 'desqualifica', 'redireciona', 'em_andamento'  -- v2 (portões)
));

alter table radar.conversas
  add column etapa            text not null default 'qualificando'
                              check (etapa in ('qualificando', 'oferta', 'esteira')),
  add column cadencia_inicio  timestamptz,
  add column esteira_desde    timestamptz,
  add column esteira_enviados smallint not null default 0 check (esteira_enviados between 0 and 10),
  add column agenda           jsonb;

comment on column radar.conversas.etapa is
  'Triagem v2: qualificando (roteiro + cadência de 5 toques) | oferta (2 horários oferecidos, aguardando escolha) | esteira (30/60/90 dias).';
comment on column radar.conversas.agenda is
  'Triagem v2: {sdr_id, opcoes: [{inicio, fim, rotulo}], tentativas, escolhido: {..., evento_id, lead_call_id, meet_url}}.';

commit;

-- Depois de aplicar: conferir por leitura (\d radar.conversas) e anotar em status/banco.md.
-- Reverter (antes de haver linhas com os valores novos):
--   alter table radar.conversas drop column etapa, drop column cadencia_inicio, drop column esteira_desde,
--     drop column esteira_enviados, drop column agenda;
--   alter table radar.conversas drop constraint conversas_decisao_check;
--   alter table radar.conversas add constraint conversas_decisao_check check (decisao in ('passar','acompanhar','descartar'));
