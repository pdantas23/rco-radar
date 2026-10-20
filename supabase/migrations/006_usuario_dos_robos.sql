-- RCO Radar — 006: usuário próprio dos robôs no banco (em vez da senha geral do Postgres)
--
-- JÁ APLICADA EM PRODUÇÃO em 2026-10-20 23:36:57 UTC (instância `supabase`, aprovada pelo Philip; senha definida na VPS, fora do repo). Acesso mínimo: tudo no schema radar; do COMERCIAL, só LEITURA das tabelas que
-- os robôs usam (nome da agência, config e chave da IA, mensagens do inbox). Nada em contatos,
-- negócios, conversas ou demais tabelas: isso passa pelas rotas internas do COMERCIAL.
-- BYPASSRLS: as policies do radar são para usuários logados (authenticated); os robôs operam
-- todas as agências. Só vale nas tabelas com GRANT abaixo.
-- A SENHA NÃO FICA AQUI: definir na aplicação com
--   alter role radar_worker password '<gerada na hora>';
-- e guardar só na variável RADAR_DATABASE_URL do serviço dos robôs.

begin;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'radar_worker') then
    create role radar_worker login bypassrls noinherit;
  end if;
end $$;

grant usage on schema radar to radar_worker;
grant select, insert, update, delete on all tables in schema radar to radar_worker;
grant usage, select on all sequences in schema radar to radar_worker;
-- tabelas que migrations futuras criarem no radar (criadas por supabase_admin)
alter default privileges for role supabase_admin in schema radar
  grant select, insert, update, delete on tables to radar_worker;
alter default privileges for role supabase_admin in schema radar
  grant usage, select on sequences to radar_worker;

grant usage on schema public to radar_worker;
grant select on public.agencies, public.agent_configs, public.agent_provider_keys, public.messages
  to radar_worker;

commit;
