-- RCO Radar — 003: escopo por AGÊNCIA (Radar vira área dentro do COMERCIAL-RCO)
--
-- JÁ APLICADA EM PRODUÇÃO em 2026-10-20 02:01:50 UTC (instância `supabase`, aprovada pelo Philip). Escrita em 2026-10-19, validada antes num Postgres 15 local descartável (container
-- radar-pg, com stubs das tabelas/funções do COMERCIAL). Aplicar só com aprovação do Philip,
-- via `docker exec -i supabase_supabase-db-1 psql -U supabase_admin` (instância `supabase`).
--
-- Premissa: TODAS as tabelas do schema radar estão vazias (conferir antes: o bloco de guarda
-- abaixo aborta a migration inteira se alguma tiver linha). Por isso os ALTER são seguros.
--
-- O que muda
--   1. Toda tabela do Radar ganha agency_id (FK para public.agencies). Nas tabelas filhas de
--      lead/pesquisa/conversa/disparo/abordagem a FK vira COMPOSTA (id, agency_id): o banco
--      impede, por constraint, uma linha filha de apontar para um lead de outra agência.
--   2. leads: place_id deixa de ser único global; passa a ser único POR agência.
--   3. Nova radar.agencias: liga/desliga, teto mensal (só RCO) e configuração (maior papel).
--   4. radar.usuarios, eh_usuario(), eh_admin() e radar.contas_envio saem. A conexão de WhatsApp
--      passa a ser a de public.whatsapp_config (por conta no COMERCIAL).
--   5. Todas as policies RLS reescritas por agência.
--
-- Quem pode o quê (decisão a confirmar com o Philip)
--   * RCO (public.is_rco()): tudo, em qualquer agência. Único que cria a linha em radar.agencias,
--     liga/desliga o Radar e define o teto mensal.
--   * Membro da agência (public.agency_members, agência 'active'): ler tudo da agência e operar
--     (criar pesquisa, editar/descartar lead, bloquear número).
--   * "Maior papel" (configurar funil/etapa, contato, WhatsApp, intervalos, janela, limites, IA,
--     abordagens, config): dono da agência (agencies.owner_user_id) OU admin/owner da CONTA MESTRE
--     da agência (is_account_member(agency_master_account_id(agency), 'admin')) — e, nos dois
--     casos, precisa ser membro da agência. Motivo: é o que já existe no COMERCIAL, não cria
--     papel novo; a conta mestre é a conta do dono, então "admin ou owner dela" é o topo real da
--     hierarquia da agência (agency_members não tem coluna de papel).

begin;

-- ── 0. Guarda: aborta se alguma tabela do radar já tiver dado ──────────────
do $$
declare t text; n bigint; total bigint := 0; lista text := '';
begin
  for t in select c.relname from pg_class c join pg_namespace s on s.oid = c.relnamespace
           where s.nspname = 'radar' and c.relkind = 'r' loop
    execute format('select count(*) from radar.%I', t) into n;
    if n > 0 then total := total + n; lista := lista || ' ' || t || '=' || n; end if;
  end loop;
  if total > 0 then
    raise exception '003 abortada: tabelas do radar com dados (%). Esta migration assume tabelas vazias.', lista;
  end if;
end $$;

-- ── 1. Tira o que depende de radar.usuarios / contas_envio ─────────────────
-- 1a. todas as policies antigas
do $$
declare r record;
begin
  for r in select schemaname, tablename, policyname from pg_policies where schemaname = 'radar' loop
    execute format('drop policy %I on %I.%I', r.policyname, r.schemaname, r.tablename);
  end loop;
end $$;

-- 1b. FKs de "quem fez" passam de radar.usuarios para auth.users (set null: apagar um usuário
--     não pode ser barrado nem apagar histórico; quem agiu fica também em radar.historico)
do $$
declare
  s record; c text;
begin
  for s in select * from (values
      ('pesquisas','usuario_id'), ('leads','editado_por'), ('regras_pontuacao','criada_por'),
      ('disparos','usuario_id'), ('conversas','assumida_por'), ('exportacoes','usuario_id'),
      ('historico','usuario_id'), ('config','atualizado_por')) v(tab, col) loop
    select con.conname into c from pg_constraint con
      where con.conrelid = format('radar.%I', s.tab)::regclass and con.contype = 'f'
        and con.confrelid = 'radar.usuarios'::regclass
        and con.conkey = array[(select attnum from pg_attribute
                                where attrelid = con.conrelid and attname = s.col)];
    if c is null then raise exception 'FK de radar.%.% para radar.usuarios não encontrada', s.tab, s.col; end if;
    execute format('alter table radar.%I drop constraint %I', s.tab, c);
    execute format('alter table radar.%I alter column %I drop not null', s.tab, s.col);
    execute format('alter table radar.%I add constraint %I foreign key (%I) references auth.users(id) on delete set null',
                   s.tab, s.tab || '_' || s.col || '_fkey', s.col);
  end loop;
end $$;

-- 1c. contas_envio: some. disparos aponta direto para a conexão do COMERCIAL.
alter table radar.disparos drop column conta_envio_id;
drop table radar.contas_envio;
drop table radar.usuarios;
drop function radar.eh_usuario();
drop function radar.eh_admin();

alter table radar.disparos
  add column whatsapp_config_id uuid references public.whatsapp_config(id) on delete set null,
  add column intervalo_max_seg  int,                       -- null = intervalo fixo (intervalo_seg)
  add column aleatorio          boolean not null default false,
  add constraint disparos_intervalo_ok check (
    intervalo_seg >= 0 and (intervalo_max_seg is null or intervalo_max_seg >= intervalo_seg));
comment on column radar.disparos.intervalo_seg is 'Intervalo mínimo (ou fixo) entre mensagens, em segundos; cópia da config da agência no momento do disparo.';
comment on column radar.disparos.intervalo_max_seg is 'Se aleatorio = true, o intervalo sorteado fica entre intervalo_seg e intervalo_max_seg.';

-- ── 2. agency_id em tudo ───────────────────────────────────────────────────
do $$
declare t text;
begin
  -- not null (tabelas vazias: sem default)
  foreach t in array array[
    'pesquisas','leads','abordagens','disparos','conversas','bloqueios','exportacoes','historico',
    'custos_api','jobs','resumo_diario','config',
    'empresas','presenca','anuncios','avaliacoes','pontuacoes','pesquisas_leads','mensagens','disparos_leads']
  loop
    execute format('alter table radar.%I add column agency_id uuid not null references public.agencies(id)', t);
  end loop;
end $$;

-- regras de pontuação: agency_id null = regra PADRÃO RCO, valendo para qualquer agência que
-- não tenha regra própria para o nicho. Só a RCO mexe nas padrão (ver policies).
alter table radar.regras_pontuacao add column agency_id uuid references public.agencies(id);
alter table radar.regras_pontuacao drop constraint regras_pontuacao_nicho_versao_key;
alter table radar.regras_pontuacao
  add constraint regras_pontuacao_unica unique nulls not distinct (agency_id, nicho, versao);

-- config: chave passa a ser por agência
alter table radar.config drop constraint config_pkey;
alter table radar.config add primary key (agency_id, chave);

-- ── 3. FKs compostas: filho não aponta para pai de outra agência ───────────
alter table radar.leads       add constraint leads_id_agencia_uk       unique (id, agency_id);
alter table radar.pesquisas   add constraint pesquisas_id_agencia_uk   unique (id, agency_id);
alter table radar.conversas   add constraint conversas_id_agencia_uk   unique (id, agency_id);
alter table radar.disparos    add constraint disparos_id_agencia_uk    unique (id, agency_id);
alter table radar.abordagens  add constraint abordagens_id_agencia_uk  unique (id, agency_id);

do $$
declare s record; c text;
begin
  for s in select * from (values
      ('pesquisas','rodada_de','pesquisas'),
      ('pesquisas_leads','pesquisa_id','pesquisas'), ('pesquisas_leads','lead_id','leads'),
      ('avaliacoes','lead_id','leads'), ('empresas','lead_id','leads'), ('presenca','lead_id','leads'),
      ('anuncios','lead_id','leads'), ('pontuacoes','lead_id','leads'),
      ('disparos','abordagem_id','abordagens'),
      ('disparos_leads','disparo_id','disparos'), ('disparos_leads','lead_id','leads'),
      ('conversas','lead_id','leads'), ('mensagens','conversa_id','conversas'),
      ('bloqueios','lead_id','leads'),
      ('custos_api','pesquisa_id','pesquisas'), ('custos_api','lead_id','leads'),
      ('jobs','pesquisa_id','pesquisas'), ('jobs','lead_id','leads'),
      ('resumo_diario','pesquisa_id','pesquisas')) v(tab, col, pai) loop
    select con.conname into c from pg_constraint con
      where con.conrelid = format('radar.%I', s.tab)::regclass and con.contype = 'f'
        and con.confrelid = format('radar.%I', s.pai)::regclass
        and con.conkey = array[(select attnum from pg_attribute where attrelid = con.conrelid and attname = s.col)];
    if c is null then raise exception 'FK radar.%.% -> radar.% não encontrada', s.tab, s.col, s.pai; end if;
    execute format('alter table radar.%I drop constraint %I', s.tab, c);
    -- MATCH SIMPLE: coluna de FK nula (lead_id opcional) continua sem checagem, como antes.
    execute format('alter table radar.%I add constraint %I foreign key (%I, agency_id) references radar.%I (id, agency_id)',
                   s.tab, s.tab || '_' || s.col || '_agencia_fkey', s.col, s.pai);
  end loop;
end $$;

-- ── 4. Unicidade e índices por agência ─────────────────────────────────────
alter table radar.leads drop constraint leads_place_id_key;
alter table radar.leads add constraint leads_agencia_place_uk unique (agency_id, place_id);

drop index radar.bloqueios_telefone_idx, radar.bloqueios_instagram_idx;
create unique index bloqueios_telefone_idx   on radar.bloqueios (agency_id, telefone) where telefone is not null;
create unique index bloqueios_instagram_idx  on radar.bloqueios (agency_id, lower(instagram_user)) where instagram_user is not null;

do $$
declare c text;
begin
  select conname into c from pg_constraint
   where conrelid = 'radar.resumo_diario'::regclass and contype = 'u';
  if c is null then raise exception 'unique de resumo_diario não encontrada'; end if;
  execute format('alter table radar.resumo_diario drop constraint %I', c);
end $$;
alter table radar.resumo_diario
  add constraint resumo_diario_unico unique nulls not distinct (agency_id, dia, metrica, pesquisa_id, nicho, estado, cidade);

drop index radar.leads_status_idx, radar.leads_cidade_idx, radar.leads_pontuacao_idx, radar.leads_telefone_idx,
           radar.pesquisas_duplicidade_idx, radar.custos_api_em_idx;
create index leads_status_idx     on radar.leads (agency_id, status);
create index leads_cidade_idx     on radar.leads (agency_id, estado, cidade);
create index leads_pontuacao_idx  on radar.leads (agency_id, pontuacao desc nulls last);
create index leads_telefone_idx   on radar.leads (agency_id, telefone);
create index pesquisas_duplicidade_idx on radar.pesquisas (agency_id, lower(nicho_texto), lugar_estado, lower(coalesce(lugar_cidade, '')), criada_em desc);
create index custos_api_em_idx    on radar.custos_api (agency_id, em);

-- índice de agency_id nas demais (RLS filtra por ele)
do $$
declare t text;
begin
  foreach t in array array['abordagens','disparos','conversas','bloqueios','exportacoes','historico','jobs',
    'resumo_diario','regras_pontuacao','empresas','presenca','anuncios','avaliacoes','pontuacoes',
    'pesquisas_leads','mensagens','disparos_leads']
  loop
    execute format('create index %I on radar.%I (agency_id)', t || '_agencia_idx', t);
  end loop;
end $$;

-- ── 5. Ligação com o COMERCIAL: lead entregue vira contato + negócio ───────
alter table radar.leads
  add column contact_id uuid references public.contacts(id) on delete set null,
  add column deal_id    uuid references public.deals(id)    on delete set null;
create index leads_contact_idx on radar.leads (contact_id) where contact_id is not null;
create index leads_deal_idx    on radar.leads (deal_id)    where deal_id is not null;
comment on column radar.leads.contact_id is 'Contato criado no COMERCIAL ao entregar o lead (conta mestre da agência).';
comment on column radar.leads.deal_id    is 'Negócio criado no COMERCIAL ao entregar o lead.';

-- ── 6. Funções de acesso ───────────────────────────────────────────────────
-- Agências que o usuário logado pode OPERAR: todas, se for RCO; senão as de que é membro
-- (agência ativa). Devolve conjunto para a policy avaliar UMA vez por consulta (initplan),
-- não uma vez por linha.
create or replace function radar.minhas_agencias() returns setof uuid
language sql stable security definer set search_path = pg_catalog, public as $$
  select a.id from public.agencies a where public.is_rco()
  union
  select am.agency_id from public.agency_members am
    join public.agencies a on a.id = am.agency_id
   where am.user_id = auth.uid() and a.status = 'active'
$$;

-- "Maior papel" da agência: RCO, ou (agência ativa, usuário membro dela e) dono da agência
-- ou admin/owner da conta mestre.
create or replace function radar.pode_configurar(p_agency uuid) returns boolean
language sql stable security definer set search_path = pg_catalog, public as $$
  select public.is_rco() or exists (
    select 1 from public.agencies a
     where a.id = p_agency and a.status = 'active'
       and public.is_agency_member(a.id)
       and (a.owner_user_id = auth.uid()
            or public.is_account_member(public.agency_master_account_id(a.id), 'admin')))
$$;

revoke execute on function radar.minhas_agencias(), radar.pode_configurar(uuid) from public, anon;
grant  execute on function radar.minhas_agencias(), radar.pode_configurar(uuid) to authenticated, service_role;

-- ── 7. radar.agencias: liga/desliga, teto e configuração ───────────────────
create table radar.agencias (
  agency_id         uuid primary key references public.agencies(id) on delete cascade,
  -- só RCO (trigger abaixo)
  ativo             boolean not null default false,
  teto_mensal_brl   numeric(12,2) not null default 0 check (teto_mensal_brl >= 0),  -- 0 = sem gasto permitido
  -- maior papel
  pipeline_id       uuid references public.pipelines(id) on delete set null,       -- funil onde o qualificado entra
  stage_id          uuid references public.pipeline_stages(id) on delete set null, -- etapa inicial
  criar_contato     boolean not null default true,
  whatsapp_config_id uuid references public.whatsapp_config(id) on delete set null, -- conexão usada nos envios
  aleatorio         boolean not null default false,        -- intervalo sorteado entre min e max
  intervalo_min_seg int not null default 90  check (intervalo_min_seg >= 0),
  intervalo_max_seg int not null default 90,
  janela_inicio     time not null default '08:00',
  janela_fim        time not null default '20:00',
  fuso              text not null default 'America/Sao_Paulo',
  limite_hora       int not null default 20 check (limite_hora > 0),
  limite_dia        int not null default 60 check (limite_dia > 0),
  comportamento_ia  jsonb not null default '{}',           -- tom, instruções, regras de follow-up
  criado_em         timestamptz not null default now(),
  atualizado_por    uuid references auth.users(id) on delete set null,
  atualizado_em     timestamptz not null default now(),
  constraint agencias_intervalo_ok check (intervalo_max_seg >= intervalo_min_seg),
  constraint agencias_etapa_exige_funil check (stage_id is null or pipeline_id is not null)
);
comment on table radar.agencias is 'Uma linha por agência que usa o Radar. Criada, ligada e com teto definidos só pela RCO; o resto o "maior papel" da agência configura.';
comment on column radar.agencias.teto_mensal_brl is 'Teto de gasto das fontes por mês (BRL). Gasto do mês: radar.gasto_mes.';

create or replace function radar.agencias_guarda() returns trigger
language plpgsql security definer set search_path = pg_catalog, public as $$
declare master uuid;
begin
  -- ativo e teto: só RCO. auth.uid() nulo = service role / admin do banco (workers, manutenção).
  if auth.uid() is not null and not public.is_rco() then
    if tg_op = 'INSERT' or new.ativo is distinct from old.ativo
       or new.teto_mensal_brl is distinct from old.teto_mensal_brl then
      raise exception 'somente a RCO liga/desliga o Radar ou altera o teto mensal';
    end if;
  end if;

  -- pipeline, etapa e conexão têm de ser da CONTA MESTRE da agência
  master := public.agency_master_account_id(new.agency_id);
  if master is null then raise exception 'agência % sem conta mestre', new.agency_id; end if;
  if new.pipeline_id is not null and not exists (
       select 1 from public.pipelines where id = new.pipeline_id and account_id = master) then
    raise exception 'funil não pertence à conta mestre da agência';
  end if;
  if new.stage_id is not null and not exists (
       select 1 from public.pipeline_stages where id = new.stage_id and pipeline_id = new.pipeline_id) then
    raise exception 'etapa não pertence ao funil escolhido';
  end if;
  if new.whatsapp_config_id is not null and not exists (
       select 1 from public.whatsapp_config where id = new.whatsapp_config_id and account_id = master) then
    raise exception 'conexão de WhatsApp não pertence à conta mestre da agência';
  end if;

  new.atualizado_em := now();
  if auth.uid() is not null then new.atualizado_por := auth.uid(); end if;
  return new;
end $$;
revoke execute on function radar.agencias_guarda() from public, anon, authenticated;

create trigger agencias_guarda before insert or update on radar.agencias
  for each row execute function radar.agencias_guarda();

-- Gasto do mês por agência (BRL), para comparar com o teto. Respeita a RLS de custos_api.
create view radar.gasto_mes with (security_invoker = true) as
select agency_id,
       date_trunc('month', em at time zone 'America/Sao_Paulo')::date as mes,
       sum(valor)        as gasto_brl,
       bool_or(estimado) as inclui_estimado
  from radar.custos_api
 group by 1, 2;

-- ── 8. Triggers ────────────────────────────────────────────────────────────
-- Histórico de status agora grava a agência.
create or replace function radar.historico_status_lead() returns trigger
language plpgsql security definer set search_path = radar as $$
begin
  if new.status is distinct from old.status then
    insert into radar.historico (agency_id, entidade, entidade_id, acao, antes, depois, usuario_id)
    values (new.agency_id, 'lead', new.id::text, 'status',
            jsonb_build_object('status', old.status),
            jsonb_build_object('status', new.status, 'motivo_descarte', new.motivo_descarte),
            auth.uid());
  end if;
  return new;
end $$;

-- agency_id nunca muda depois de criado (evita mover linha entre agências por UPDATE).
create or replace function radar.travar_agencia() returns trigger
language plpgsql as $$
begin
  if new.agency_id is distinct from old.agency_id then
    raise exception 'agency_id não pode ser alterado';
  end if;
  return new;
end $$;

do $$
declare t text;
begin
  foreach t in array array[
    'pesquisas','leads','abordagens','disparos','conversas','bloqueios','exportacoes','historico',
    'custos_api','jobs','resumo_diario','config','regras_pontuacao','empresas','presenca','anuncios',
    'avaliacoes','pontuacoes','pesquisas_leads','mensagens','disparos_leads','agencias']
  loop
    execute format('create trigger %I before update on radar.%I for each row execute function radar.travar_agencia()',
                   t || '_trava_agencia', t);
  end loop;
end $$;
-- (agencias usa agency_id como PK; a coluna é a mesma, a função serve.)

-- Funções de trigger não precisam de EXECUTE para ninguém (o trigger roda sem checar o chamador).
revoke execute on function radar.historico_status_lead(), radar.travar_agencia(),
  radar.tocar_atualizado_em(), radar.proibir_delete() from public, anon, authenticated;

-- ── 9. RLS ─────────────────────────────────────────────────────────────────
do $$
declare t text;
begin
  -- operação: qualquer membro da agência (ou RCO) lê
  foreach t in array array['pesquisas','leads','pesquisas_leads','avaliacoes','empresas','presenca',
    'anuncios','pontuacoes','disparos','disparos_leads','conversas','mensagens','bloqueios',
    'exportacoes','historico','custos_api','jobs','resumo_diario']
  loop
    execute format('alter table radar.%I enable row level security', t);
    execute format('create policy %I on radar.%I for select to authenticated using (agency_id in (select radar.minhas_agencias()))',
                   t || '_ler', t);
  end loop;

  -- configuração: membro lê; só o maior papel (ou RCO) escreve
  foreach t in array array['abordagens','config']
  loop
    execute format('alter table radar.%I enable row level security', t);
    execute format('create policy %I on radar.%I for select to authenticated using (agency_id in (select radar.minhas_agencias()))',
                   t || '_ler', t);
    execute format('create policy %I on radar.%I for all to authenticated using (radar.pode_configurar(agency_id)) with check (radar.pode_configurar(agency_id))',
                   t || '_config', t);
  end loop;
end $$;

-- escritas do operador pela tela (as demais passam por rotas do servidor/workers, service role)
create policy pesquisas_criar on radar.pesquisas for insert to authenticated
  with check (agency_id in (select radar.minhas_agencias()) and usuario_id = auth.uid());
create policy leads_editar on radar.leads for update to authenticated
  using (agency_id in (select radar.minhas_agencias()))
  with check (agency_id in (select radar.minhas_agencias()));
create policy bloqueios_criar on radar.bloqueios for insert to authenticated
  with check (agency_id in (select radar.minhas_agencias()));

-- regras de pontuação: membro lê as da agência e as padrão (agency_id null);
-- agência edita só as próprias; as padrão só a RCO.
alter table radar.regras_pontuacao enable row level security;
create policy regras_ler on radar.regras_pontuacao for select to authenticated
  using (agency_id is null or agency_id in (select radar.minhas_agencias()));
create policy regras_config on radar.regras_pontuacao for all to authenticated
  using (agency_id is not null and radar.pode_configurar(agency_id))
  with check (agency_id is not null and radar.pode_configurar(agency_id));
create policy regras_padrao_rco on radar.regras_pontuacao for all to authenticated
  using (agency_id is null and public.is_rco())
  with check (agency_id is null and public.is_rco());

-- agencias: membro lê; RCO cria/apaga; maior papel atualiza (trigger trava ativo/teto)
alter table radar.agencias enable row level security;
create policy agencias_ler on radar.agencias for select to authenticated
  using (agency_id in (select radar.minhas_agencias()));
create policy agencias_criar on radar.agencias for insert to authenticated
  with check (public.is_rco());
create policy agencias_apagar on radar.agencias for delete to authenticated
  using (public.is_rco());
create policy agencias_config on radar.agencias for update to authenticated
  using (radar.pode_configurar(agency_id)) with check (radar.pode_configurar(agency_id));

-- ── 10. Grants ─────────────────────────────────────────────────────────────
-- (os grants antigos de usuarios/contas_envio sumiram junto com as tabelas)
grant select on all tables in schema radar to authenticated;   -- inclui agencias e a view gasto_mes
grant insert on radar.pesquisas, radar.bloqueios to authenticated;
grant update on radar.leads to authenticated;
-- configuração: o GRANT abre, a policy restringe ao maior papel / RCO
grant insert, update, delete on radar.regras_pontuacao, radar.abordagens, radar.config,
  radar.agencias to authenticated;
grant usage, select on all sequences in schema radar to authenticated;
grant all on all tables in schema radar to service_role;
grant usage, select on all sequences in schema radar to service_role;

commit;

-- Depois de aplicar:
--   * conferir por leitura (pg_policies, \d radar.agencias) e anotar em status/banco.md;
--   * `radar` ainda precisa estar em PGRST_DB_SCHEMAS do PostgREST (infra) para o front ler via REST;
--   * COMERCIAL-RCO: o app usa as tabelas radar.* pela sessão do usuário; pipeline/etapa/conexão
--     gravados em radar.agencias têm de ser da conta mestre (o trigger recusa o contrário).
