-- Stub MÍNIMO do COMERCIAL-RCO (schema public) só para rodar o app local com o Radar (migration 003). Nunca produção.
do $$ begin
  if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin bypassrls; end if;
  if not exists (select 1 from pg_roles where rolname='authenticator') then create role authenticator noinherit login password 'x'; end if;
end $$;
grant anon, authenticated, service_role to authenticator;
create schema auth; create table auth.users (id uuid primary key, email text);
create function auth.uid() returns uuid language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim.sub', true), ''),
                  (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'))::uuid $$;
grant usage on schema auth to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;
create extension if not exists pgcrypto;

create table accounts (id uuid primary key default gen_random_uuid(), name text not null, agency_id uuid, default_currency text default 'BRL', owner_user_id uuid);
create table agencies (id uuid primary key default gen_random_uuid(), name text not null, owner_user_id uuid not null, status text not null default 'active', is_platform boolean not null default false);
create table agency_members (agency_id uuid not null references agencies(id) on delete cascade, user_id uuid not null, primary key (agency_id, user_id));
create table profiles (id uuid primary key default gen_random_uuid(), user_id uuid unique not null, full_name text, email text, avatar_url text, role text default 'user',
  beta_features jsonb default '{}', account_id uuid references accounts(id), account_role text default 'agent', job_function text, platform_role text);
create table account_branding (account_id uuid primary key, app_name text, logo_url text, primary_color text);
create table pipelines (id uuid primary key default gen_random_uuid(), account_id uuid not null references accounts(id), user_id uuid, name text not null, commercial_role text);
create table pipeline_stages (id uuid primary key default gen_random_uuid(), pipeline_id uuid not null references pipelines(id) on delete cascade, name text not null, position int not null default 0);
create table whatsapp_config (id uuid primary key default gen_random_uuid(), account_id uuid not null references accounts(id), label text, connected_phone_number text, status text default 'connected', connection_type text default 'baileys');
create table contacts (id uuid primary key default gen_random_uuid(), account_id uuid references accounts(id), name text, phone text, lead_source text, is_test boolean not null default false, is_group boolean not null default false, assigned_to uuid, created_at timestamptz not null default now());
create table deals (id uuid primary key default gen_random_uuid(), account_id uuid references accounts(id), user_id uuid, pipeline_id uuid, stage_id uuid, contact_id uuid references contacts(id), title text, value numeric default 0, status text not null default 'open', is_test boolean not null default false, won_at timestamptz, created_at timestamptz not null default now());
create table lead_timeline_events (id uuid primary key default gen_random_uuid(), account_id uuid, contact_id uuid, event_type text, created_at timestamptz not null default now());
create table messages (id uuid primary key default gen_random_uuid(), conversation_id uuid, direction text, sender_type text, content text, created_at timestamptz not null default now());
create table conversations (id uuid primary key default gen_random_uuid(), account_id uuid, contact_id uuid, status text not null default 'open', first_responded_at timestamptz, unread_count int default 0, assigned_to uuid, created_at timestamptz not null default now(), updated_at timestamptz default now());
alter table messages add constraint messages_conversation_fk foreign key (conversation_id) references conversations(id) on delete cascade;
create table member_presence (user_id uuid primary key, account_id uuid, last_seen_at timestamptz);
create table audit_log (id uuid primary key default gen_random_uuid(), account_id uuid not null, actor_user_id uuid, action text, resource_type text, resource_id text, metadata jsonb default '{}', created_at timestamptz default now());

create function update_updated_at_column() returns trigger language plpgsql as $$ begin new.updated_at = now(); return new; end $$;
create function is_rco() returns boolean language sql stable security definer as $$ select exists (select 1 from profiles where user_id = auth.uid() and platform_role = 'rco') $$;
create function is_agency_member(target_agency_id uuid) returns boolean language sql stable security definer as $$
  select exists (select 1 from agency_members where agency_id = target_agency_id and user_id = auth.uid()) $$;
create function agency_master_account_id(target_agency_id uuid) returns uuid language sql stable security definer as $$
  select p.account_id from agencies ag join profiles p on p.user_id = ag.owner_user_id where ag.id = target_agency_id $$;
create function is_account_member(target_account uuid, min_role text) returns boolean language sql stable security definer as $$
  select exists (select 1 from profiles where user_id = auth.uid() and account_id = target_account and account_role in ('owner','admin')) $$;
grant usage on schema public to anon, authenticated, service_role;
grant all on all tables in schema public to authenticated, service_role;
grant execute on all functions in schema public to anon, authenticated, service_role;

-- MODELO REAL (como em produção): a RCO tem uma conta "casa" (Desenvolvimento RCO) e entra na agência trocando o seletor.
--   Luis  = RCO (platform_role), conta casa "Desenvolvimento RCO", membro das agências  -> "Plataforma RCO" ao logar
--   Dono  = dono da agência RCO HUB, conta casa "RCO HUB" (= conta-mestre da agência)      -> maior papel da agência
--   Ana   = vendedora (agent) na conta-mestre RCO HUB
insert into auth.users (id, email) values ('11111111-1111-1111-1111-111111111111','luis@rcohub.dev'),('22222222-2222-2222-2222-222222222222','ana@rcohub.dev'),('44444444-4444-4444-4444-444444444444','dono@rcohub.dev');
insert into accounts (id, name, owner_user_id) values
 ('cccccccc-0000-0000-0000-000000000001','RCO HUB','44444444-4444-4444-4444-444444444444'),
 ('cccccccc-0000-0000-0000-000000000002','Desenvolvimento RCO','11111111-1111-1111-1111-111111111111');
insert into agencies (id, name, owner_user_id, is_platform) values
 ('aaaaaaaa-1111-0000-0000-000000000001','RCO HUB','44444444-4444-4444-4444-444444444444', false),
 ('aaaaaaaa-2222-0000-0000-000000000009','Plataforma RCO','11111111-1111-1111-1111-111111111111', true);
insert into agency_members values
 ('aaaaaaaa-1111-0000-0000-000000000001','44444444-4444-4444-4444-444444444444'),('aaaaaaaa-1111-0000-0000-000000000001','22222222-2222-2222-2222-222222222222'),('aaaaaaaa-1111-0000-0000-000000000001','11111111-1111-1111-1111-111111111111'),
 ('aaaaaaaa-2222-0000-0000-000000000009','11111111-1111-1111-1111-111111111111');
insert into profiles (user_id, full_name, email, account_id, account_role, platform_role) values
 ('11111111-1111-1111-1111-111111111111','Luis Coelho','luis@rcohub.dev','cccccccc-0000-0000-0000-000000000002','owner','rco'),
 ('44444444-4444-4444-4444-444444444444','Philip Dantas','dono@rcohub.dev','cccccccc-0000-0000-0000-000000000001','owner',null),
 ('22222222-2222-2222-2222-222222222222','Ana Souza','ana@rcohub.dev','cccccccc-0000-0000-0000-000000000001','agent',null);
insert into pipelines (id, account_id, name) values ('dddddddd-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','Prospecção Radar'),('dddddddd-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','Vendas');
insert into pipeline_stages (pipeline_id, name, position) values
 ('dddddddd-0000-0000-0000-000000000001','Novo lead',0),('dddddddd-0000-0000-0000-000000000001','Em conversa',1),('dddddddd-0000-0000-0000-000000000001','Reunião marcada',2),
 ('dddddddd-0000-0000-0000-000000000002','Proposta',0);
insert into whatsapp_config (id, account_id, label, connected_phone_number) values
 ('eeeeeeee-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','Radar nº 1','5586900000001'),
 ('eeeeeeee-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','Radar nº 2','5586900000002');
create function touch_presence(p_status text default 'online') returns void language sql as $$ select $$;
grant execute on function touch_presence(text) to authenticated, service_role;

create function rco_dashboard_metrics() returns jsonb language sql stable security definer as $$
  select jsonb_build_object(
    'agencies_total', (select count(*) from agencies),
    'agencies_active', (select count(*) from agencies where status = 'active'),
    'agencies_suspended', (select count(*) from agencies where status <> 'active'),
    'accounts_total', (select count(*) from accounts),
    'users_total', (select count(*) from profiles),
    'leads_total', 0, 'conversations_total', 0, 'sales_total', 0, 'revenue_total', 0) $$;
grant execute on function rco_dashboard_metrics() to authenticated, service_role;

-- mais lacunas do Painel e das Tasks (existem em produção; aqui só o mínimo para o dev não mostrar erro)
create table lead_calls (id uuid primary key default gen_random_uuid(), account_id uuid, contact_id uuid, status text, created_at timestamptz not null default now());
create table conversion_events (id uuid primary key default gen_random_uuid(), account_id uuid, value numeric default 0, occurred_at timestamptz not null default now());
create table ad_spend (id uuid primary key default gen_random_uuid(), account_id uuid, amount numeric default 0, spend_date date not null default current_date);
create table task_statuses (id uuid primary key default gen_random_uuid(), account_id uuid not null references accounts(id) on delete cascade, name text not null, color text not null default '#64748b', position int not null default 0, status_group text not null check (status_group in ('not_started','in_progress','done')), created_at timestamptz not null default now(), updated_at timestamptz not null default now());
create unique index idx_task_statuses_account_name on task_statuses (account_id, lower(btrim(name)));
create table task_groups (id uuid primary key default gen_random_uuid(), account_id uuid not null references accounts(id) on delete cascade, name text not null, created_at timestamptz not null default now(), updated_at timestamptz not null default now());
create unique index idx_task_groups_id_account on task_groups (id, account_id);
create table task_group_members (group_id uuid not null, account_id uuid not null, user_id uuid not null references auth.users(id) on delete cascade, created_at timestamptz not null default now(), primary key (group_id, user_id), foreign key (group_id, account_id) references task_groups (id, account_id) on delete cascade);
create table tasks (id uuid primary key default gen_random_uuid(), account_id uuid not null references accounts(id) on delete cascade, title text not null, description text,
  assigned_to uuid references auth.users(id) on delete set null, assigned_group_kind text, assigned_group_id uuid, created_by uuid references auth.users(id) on delete set null, due_date date,
  priority text not null default 'normal', status_id uuid not null, completed_at timestamptz, goal_type text not null default 'none', goal_target numeric(14,2), goal_current numeric(14,2) not null default 0, goal_unit text,
  contact_id uuid references contacts(id) on delete set null, deal_id uuid references deals(id) on delete set null, last_change_at timestamptz not null default now(), last_change_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(), unique (id, account_id));
create table task_checklist_items (id uuid primary key default gen_random_uuid(), account_id uuid not null, task_id uuid not null, label text not null, done boolean not null default false, done_at timestamptz, done_by uuid references auth.users(id) on delete set null, position int not null default 0, created_at timestamptz not null default now(), foreign key (task_id, account_id) references tasks (id, account_id) on delete cascade);
create table task_comments (id uuid primary key default gen_random_uuid(), account_id uuid not null, task_id uuid not null, author_id uuid references auth.users(id) on delete set null, body text not null, created_at timestamptz not null default now(), foreign key (task_id, account_id) references tasks (id, account_id) on delete cascade);
create table task_attachments (id uuid primary key default gen_random_uuid(), account_id uuid not null, task_id uuid not null, storage_path text not null unique, file_name text not null, mime_type text, size_bytes bigint not null default 0, uploaded_by uuid references auth.users(id) on delete set null, created_at timestamptz not null default now(), foreign key (task_id, account_id) references tasks (id, account_id) on delete cascade);
create table task_seen (task_id uuid not null references tasks(id) on delete cascade, user_id uuid not null references auth.users(id) on delete cascade, seen_at timestamptz not null default now(), primary key (task_id, user_id));
-- status padrão das Tasks da conta-mestre (estrutural; sem tasks de exemplo)
insert into task_statuses (id, account_id, name, color, position, status_group) values
 ('f0000000-0000-0000-0000-000000000001','cccccccc-0000-0000-0000-000000000001','A fazer','#64748b',0,'not_started'),
 ('f0000000-0000-0000-0000-000000000002','cccccccc-0000-0000-0000-000000000001','Em andamento','#3b82f6',1,'in_progress'),
 ('f0000000-0000-0000-0000-000000000003','cccccccc-0000-0000-0000-000000000001','Concluída','#22c55e',2,'done');
grant all on all tables in schema public to authenticated, service_role;

-- Painel do vendedor (Ana): lacunas do stub (existem em produção)
alter table deals add column assigned_to uuid, add column updated_at timestamptz not null default now(), add column closed_at timestamptz;
alter table deals add constraint deals_stage_fk foreign key (stage_id) references pipeline_stages(id);
alter table conversations add column assigned_agent_id uuid;
create table sales_goals (id uuid primary key default gen_random_uuid(), account_id uuid, user_id uuid, target_revenue numeric default 0, target_sales int default 0, period_month date);
grant all on all tables in schema public to authenticated, service_role;

-- setores e presença (migrations 024 e 029 do COMERCIAL): usados pela config de WhatsApp e pelo indicador de online
create table if not exists sectors (id uuid primary key default gen_random_uuid(), account_id uuid not null, name text not null,
  description text, created_at timestamptz not null default now(), updated_at timestamptz not null default now());
create table if not exists sector_members (sector_id uuid not null references sectors(id) on delete cascade, user_id uuid not null,
  created_at timestamptz not null default now(), primary key (sector_id, user_id));
alter table conversations add column if not exists sector_id uuid references sectors(id) on delete set null;
alter table member_presence add column if not exists status text not null default 'online';
grant all on all tables in schema public to authenticated, service_role;
-- histórico de chamadas (migration 123 do COMERCIAL, copiada): vazio
create table whatsapp_calls (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id) on delete cascade,
  whatsapp_config_id uuid references whatsapp_config(id) on delete set null,
  contact_id uuid references contacts(id) on delete set null,
  call_id text not null,
  direction text not null default 'inbound' check (direction in ('inbound','outbound')),
  caller_jid text, caller_phone text, group_jid text, is_video boolean, remote_platform text, remote_version text,
  offered_at timestamptz, accepted_at timestamptz, terminated_at timestamptz, terminate_reason text,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint whatsapp_calls_account_call_uniq unique (account_id, call_id));
create index idx_whatsapp_calls_account_offered on whatsapp_calls (account_id, offered_at desc);
alter table whatsapp_calls enable row level security;
create policy whatsapp_calls_owner_select on whatsapp_calls for select
  using (exists (select 1 from profiles p where p.user_id = auth.uid() and p.account_id = whatsapp_calls.account_id and p.account_role = 'owner'));
grant select on whatsapp_calls to authenticated; grant all on whatsapp_calls to service_role;
-- Fase 3 (inbox + envio): colunas reais de produção que as rotas internas do Radar usam
alter table whatsapp_config add column if not exists user_id uuid, add column if not exists evolution_instance_token text,
  add column if not exists is_primary boolean not null default false, add column if not exists assigned_agency_user_id uuid, add column if not exists sector_id uuid;
alter table contacts add column if not exists user_id uuid, add column if not exists wa_lid text, add column if not exists updated_at timestamptz default now();
alter table conversations add column if not exists whatsapp_config_id uuid references whatsapp_config(id), add column if not exists user_id uuid, add column if not exists is_group boolean not null default false,
  add column if not exists last_message_text text, add column if not exists last_message_at timestamptz, add column if not exists last_message_sender_type text,
  add column if not exists assigned_at timestamptz, add column if not exists escalation_count int not null default 0, add column if not exists locked_at timestamptz;
alter table conversations add constraint conversations_contact_fk foreign key (contact_id) references contacts(id) on delete cascade;
create unique index if not exists idx_conversations_account_contact_config on conversations (account_id, contact_id, whatsapp_config_id);
alter table messages add column if not exists content_type text default 'text', add column if not exists content_text text, add column if not exists message_id text,
  add column if not exists status text, add column if not exists metadata jsonb, add column if not exists media_url text, add column if not exists media_storage_path text,
  add column if not exists media_expires_at timestamptz, add column if not exists reply_to_message_id uuid, add column if not exists interactive_reply_id text,
  add column if not exists origin_kind text, add column if not exists origin_source text, add column if not exists origin_campaign text, add column if not exists template_name text;
create unique index if not exists idx_messages_conversation_message_id on messages (conversation_id, message_id) where message_id is not null;
alter table deals add column if not exists conversation_id uuid references conversations(id), add column if not exists source text;
alter table lead_timeline_events add column if not exists payload jsonb default '{}', add column if not exists actor_user_id uuid, add column if not exists conversation_id uuid, add column if not exists dedup_key text;
create unique index if not exists idx_timeline_dedup on lead_timeline_events (dedup_key);
create table if not exists business_hours (id uuid primary key default gen_random_uuid(), account_id uuid not null, sector_id uuid, timezone text default 'UTC', schedule jsonb default '{}',
  enabled boolean default false, away_message text, first_response_target_minutes int, escalation_enabled boolean default false, escalation_seconds int, distribution_enabled boolean default false);
grant all on all tables in schema public to authenticated, service_role;
notify pgrst, 'reload schema';
