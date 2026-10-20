-- RCO Radar — schema inicial (modelo de dados mínimo do levantamento v1.0, 17/10/2026)
--
-- JÁ APLICADA EM PRODUÇÃO (instância `supabase`, 2026-10-20 01:10:30 UTC, aprovada pelo Philip). Destino: instância `supabase` (a mesma do COMERCIAL-RCO), schema
-- próprio `radar`, sem tocar em nenhuma tabela existente. Aplicar só com
-- aprovação do Philip, via `docker exec -i supabase_supabase-db-1 psql -U supabase_admin`.
--
-- Convenções:
--   * Nomes de campo e valores de status exatamente como no levantamento.
--   * "Nenhum campo vazio sem explicação": cada grupo coletado tem uma coluna
--     *_status ('encontrado' | 'nao_encontrado' | 'nao_verificado' | 'nao_se_aplica')
--     e cada dado guarda fonte + data da coleta.
--   * Texto com CHECK em vez de ENUM: trocar a lista é um ALTER simples.

begin;

create schema if not exists radar;

-- ── Usuários do Radar (perfis sobre o Supabase Auth da instância) ──────────
create table radar.usuarios (
  id            uuid primary key references auth.users(id) on delete cascade,
  nome          text not null,
  perfil        text not null default 'operador' check (perfil in ('admin', 'operador')),
  limite_diario_custo numeric(10,2),            -- F1: limite diário de gasto por usuário (null = padrão da config)
  ativo         boolean not null default true,
  criado_em     timestamptz not null default now()
);

create or replace function radar.eh_usuario() returns boolean
language sql stable security definer set search_path = radar as $$
  select exists (select 1 from radar.usuarios where id = auth.uid() and ativo)
$$;

create or replace function radar.eh_admin() returns boolean
language sql stable security definer set search_path = radar as $$
  select exists (select 1 from radar.usuarios where id = auth.uid() and ativo and perfil = 'admin')
$$;

-- ── Configuração geral (chave/valor; chaves de API ficam fora daqui) ──────
create table radar.config (
  chave         text primary key,
  valor         jsonb not null,
  atualizado_por uuid references radar.usuarios(id),
  atualizado_em timestamptz not null default now()
);

-- ── F1 · Pesquisas ─────────────────────────────────────────────────────────
create table radar.pesquisas (
  id              uuid primary key default gen_random_uuid(),
  nome            text not null,
  nicho_texto     text not null,
  categoria_maps  text,
  lugar_estado    char(2) not null,             -- estado sempre obrigatório (cidades homônimas)
  lugar_cidade    text,
  lugar_bairro    text,
  lugar_lat       double precision,
  lugar_lng       double precision,
  raio_km         numeric(6,2),
  filtros         jsonb not null default '{}',  -- max_resultados, nota_minima, min_avaliacoes
  fonte_maps      text not null,                -- adaptador usado (ex.: google_places, outscraper)
  usuario_id      uuid not null references radar.usuarios(id),
  criada_em       timestamptz not null default now(),
  status          text not null default 'rascunho'
                  check (status in ('rascunho', 'na_fila', 'minerando', 'enriquecendo', 'concluida', 'cancelada', 'falhou')),
  custo_estimado  numeric(10,4) not null default 0,
  custo_real      numeric(10,4) not null default 0,
  total_encontrado int not null default 0,
  total_aprovado   int not null default 0,
  rodada_de       uuid references radar.pesquisas(id),  -- "rodar de novo"
  -- nunca busca sem lugar: cidade, ou ponto com raio
  constraint pesquisa_tem_lugar check (lugar_cidade is not null or (lugar_lat is not null and lugar_lng is not null and raio_km is not null))
);
create index pesquisas_duplicidade_idx on radar.pesquisas (lower(nicho_texto), lugar_estado, lower(coalesce(lugar_cidade, '')), criada_em desc);

-- ── F2 · Leads (único por place_id) ────────────────────────────────────────
create table radar.leads (
  id                    uuid primary key default gen_random_uuid(),
  place_id              text not null unique,
  nome                  text not null,
  categoria             text,
  nicho                 text,                    -- editável pelo humano (F8)
  endereco              text not null,
  cidade                text,
  estado                char(2),
  cep                   text,
  lat                   double precision,
  lng                   double precision,
  telefone              text,                    -- E.164 (+55...)
  tem_whatsapp_provavel boolean not null default false,
  whatsapp              text,                    -- número exato quando vier de link wa.me
  site                  text,
  nota                  numeric(2,1),
  qtd_avaliacoes        int,
  horario               jsonb,
  link_maps             text not null,
  status_maps           text not null default 'aberto' check (status_maps in ('aberto', 'fechado_temporariamente')),
  fonte_maps            text not null,
  coletado_maps_em      timestamptz not null default now(),

  status                text not null default 'encontrado' check (status in (
                          'encontrado', 'enriquecido', 'pontuado', 'aprovado_para_contato',
                          'contatado', 'respondeu', 'qualificado', 'entregue', 'descartado')),
  motivo_descarte       text check (motivo_descarte in (
                          'duplicado', 'fora_do_perfil', 'sem_contato', 'pediu_para_nao_receber',
                          'numero_invalido', 'outro')),
  motivo_descarte_texto text,
  sem_resposta          boolean not null default false,   -- F10: esgotou os follow-ups
  revisao_cnpj          boolean not null default false,   -- F3: mais de um candidato

  classificacao         text check (classificacao in ('quente', 'morno', 'frio')),
  pontuacao             smallint check (pontuacao between 0 and 100),
  versao_regra          int,

  faturamento_estimado_faixa  text check (faturamento_estimado_faixa in (
                          'ate_30k', '30k_80k', '80k_200k', '200k_500k', 'acima_500k', 'sem_estimativa')),
  faturamento_origem    text check (faturamento_origem in ('base_externa', 'calculado')),
  faturamento_explicacao text,                   -- "Estimado pelo sistema a partir de: ..."
  faturamento_informado_faixa text check (faturamento_informado_faixa in (
                          'ate_30k', '30k_80k', '80k_200k', '200k_500k', 'acima_500k')),

  presenca_digital      text check (presenca_digital in ('nenhuma', 'fraca', 'media', 'forte', 'nao_verificado')),
  anuncio_ativo         text not null default 'nao_verificado' check (anuncio_ativo in ('sim', 'nao', 'nao_verificado')),
  qtd_anuncios_ativos   int,

  cnpj_status           text not null default 'nao_verificado' check (cnpj_status in ('encontrado', 'nao_encontrado', 'nao_verificado', 'revisao')),
  instagram_status      text not null default 'nao_verificado' check (instagram_status in ('encontrado', 'nao_encontrado', 'nao_verificado')),
  site_status           text not null default 'nao_verificado' check (site_status in ('encontrado', 'nao_encontrado', 'nao_verificado', 'nao_se_aplica')),

  observacao_humana     text,
  exportado_remarketing_em timestamptz,
  retorno_comercial     jsonb,                   -- F12: fora da v1, campo reservado
  editado_por           uuid references radar.usuarios(id),  -- F8: aviso de edição recente
  editado_em            timestamptz,
  criado_em             timestamptz not null default now(),
  atualizado_em         timestamptz not null default now(),

  constraint descarte_tem_motivo check ((status = 'descartado') = (motivo_descarte is not null)),
  constraint outro_tem_texto check (motivo_descarte is distinct from 'outro' or motivo_descarte_texto is not null)
);
create index leads_status_idx on radar.leads (status);
create index leads_cidade_idx on radar.leads (estado, cidade);
create index leads_pontuacao_idx on radar.leads (pontuacao desc nulls last);
create index leads_telefone_idx on radar.leads (telefone);

create table radar.pesquisas_leads (
  pesquisa_id   uuid not null references radar.pesquisas(id),
  lead_id       uuid not null references radar.leads(id),
  encontrado_em timestamptz not null default now(),
  primary key (pesquisa_id, lead_id)
);
create index pesquisas_leads_lead_idx on radar.pesquisas_leads (lead_id);

create table radar.avaliacoes (               -- até 5 por lead (regra aplicada no worker)
  id        bigint generated always as identity primary key,
  lead_id   uuid not null references radar.leads(id),
  autor     text,
  nota      smallint,
  texto     text,
  data      timestamptz
);
create index avaliacoes_lead_idx on radar.avaliacoes (lead_id);

-- ── F3 · Empresas (CNPJ) — candidatos ficam todos; escolhido = true no casado ─
create table radar.empresas (
  id                bigint generated always as identity primary key,
  lead_id           uuid not null references radar.leads(id),
  escolhido         boolean not null default false,
  cnpj              char(14) not null,
  razao_social      text,
  nome_fantasia     text,
  situacao          text,
  abertura          date,
  porte             text check (porte in ('MEI', 'ME', 'EPP', 'DEMAIS')),
  natureza          text,
  capital_social    numeric(16,2),
  cnae_principal    text,
  cnae_descricao    text,
  cnaes_secundarios jsonb,
  regime            text,
  qtd_socios        int,
  socios            jsonb,                      -- só nome e qualificação, nunca CPF
  email_rf          text,
  telefone_rf       text,
  qtd_filiais       int,
  eh_filial         boolean,
  cnpj_matriz       char(14),                   -- agrupar franquias/redes
  confianca_cnpj    smallint not null check (confianca_cnpj between 0 and 100),
  motivo_confianca  text not null,
  fonte             text not null,
  coletado_em       timestamptz not null default now(),
  -- não assumir CNPJ com confiança abaixo de 50
  constraint escolhido_confiavel check (not escolhido or confianca_cnpj >= 50)
);
create unique index empresas_um_escolhido on radar.empresas (lead_id) where escolhido;
create index empresas_lead_idx on radar.empresas (lead_id);
create index empresas_matriz_idx on radar.empresas (cnpj_matriz);

-- ── F5 · Presença digital (uma linha por coleta; a mais recente vale) ──────
create table radar.presenca (
  id                   bigint generated always as identity primary key,
  lead_id              uuid not null references radar.leads(id),
  instagram_user       text,
  instagram_origem     text check (instagram_origem in ('ficha_maps', 'site', 'busca_nome', 'humano')),
  instagram_confianca  smallint check (instagram_confianca between 0 and 100),
  seguidores           int,
  seguindo             int,
  posts_total          int,
  ultimo_post_em       timestamptz,
  posts_30d            int,
  bio                  text,
  bio_tem_link         boolean,
  bio_tem_whatsapp     boolean,
  conta_comercial      boolean,
  facebook_pagina      text,
  site_existe          boolean,
  site_responde        boolean,
  site_https           boolean,
  site_tem_whatsapp    boolean,
  site_tem_meta_pixel  boolean,
  site_tem_google_tag  boolean,
  site_tem_google_ads  boolean,
  site_plataforma      text,
  fonte_instagram      text,
  fonte_site           text,
  coletado_em          timestamptz not null default now()
);
create index presenca_lead_idx on radar.presenca (lead_id, coletado_em desc);

-- ── F6 · Anúncios (Biblioteca da Meta; plataforma prevê Google Ads depois) ─
create table radar.anuncios (
  id              bigint generated always as identity primary key,
  lead_id         uuid not null references radar.leads(id),
  plataforma      text not null default 'meta' check (plataforma in ('meta', 'google')),
  pagina_id       text,
  pagina_nome     text,
  qtd_ativos      int,                          -- null = não verificado
  mais_antigo_em  date,
  link_biblioteca text,
  amostras        jsonb,                        -- até 3: [{texto, link_criativo}]
  fonte           text not null,
  verificado_em   timestamptz not null default now()
);
create index anuncios_lead_idx on radar.anuncios (lead_id, verificado_em desc);

-- ── F7 · Pontuação (regras versionadas por nicho) ──────────────────────────
create table radar.regras_pontuacao (
  id          bigint generated always as identity primary key,
  versao      int not null,
  nicho       text,                             -- null = geral
  pesos       jsonb not null,
  faixas      jsonb not null,                   -- {quente: 70, morno: 40}
  criada_por  uuid references radar.usuarios(id),
  criada_em   timestamptz not null default now(),
  unique (nicho, versao)
);

create table radar.pontuacoes (
  id           bigint generated always as identity primary key,
  lead_id      uuid not null references radar.leads(id),
  versao_regra int not null,
  regra_id     bigint not null references radar.regras_pontuacao(id),
  criterios    jsonb not null,                  -- [{nome, bateu, pontos}]
  total        smallint not null check (total between 0 and 100),
  calculada_em timestamptz not null default now()
);
create index pontuacoes_lead_idx on radar.pontuacoes (lead_id, calculada_em desc);

-- ── F9/F10 · Abordagens, contas de envio, disparos ─────────────────────────
create table radar.abordagens (
  id            bigint generated always as identity primary key,
  nome          text not null,
  canal         text not null check (canal in ('whatsapp_oficial', 'whatsapp_nao_oficial', 'instagram')),
  texto         text not null,
  variaveis     text[] not null default '{}',
  aprovado_meta boolean not null default false, -- WABA só envia template aprovado
  template_meta text,                           -- nome do template na Meta
  ativo         boolean not null default true
);

create table radar.contas_envio (
  id                bigint generated always as identity primary key,
  tipo              text not null check (tipo in ('whatsapp_oficial', 'whatsapp_nao_oficial', 'instagram')),
  identificador     text not null unique,
  rotulo            text,
  limite_hora       int not null default 20,
  limite_dia        int not null default 60,
  intervalo_min_seg int not null default 90,
  janela_inicio     time not null default '08:00',
  janela_fim        time not null default '20:00',
  ativo             boolean not null default true,
  saude             text not null default 'ok' check (saude in ('ok', 'alerta', 'bloqueada'))
);

create table radar.disparos (
  id               bigint generated always as identity primary key,
  usuario_id       uuid not null references radar.usuarios(id),
  conta_envio_id   bigint not null references radar.contas_envio(id),
  abordagem_id     bigint not null references radar.abordagens(id),
  filtro           jsonb not null,
  qtd              int not null,
  intervalo_seg    int not null,
  limite_hora      int not null,
  confirmado_em    timestamptz,                 -- nada sai sem esse clique
  status           text not null default 'fila' check (status in ('fila', 'pausado', 'concluido', 'cancelado'))
);

create table radar.disparos_leads (
  id            bigint generated always as identity primary key,
  disparo_id    bigint not null references radar.disparos(id),
  lead_id       uuid not null references radar.leads(id),
  status        text not null default 'pendente' check (status in ('pendente', 'enviando', 'enviado', 'falhou', 'cancelado')),
  motivo_falha  text,
  agendado_para timestamptz,
  enviado_em    timestamptz,
  unique (disparo_id, lead_id)
);
create index disparos_leads_fila_idx on radar.disparos_leads (status, agendado_para);

create table radar.conversas (                -- uma por lead, canal marcado em cada mensagem
  id               uuid primary key default gen_random_uuid(),
  lead_id          uuid not null unique references radar.leads(id),
  estado           text not null default 'ia' check (estado in ('ia', 'humano', 'encerrada')),
  assumida_por     uuid references radar.usuarios(id),
  assumida_em      timestamptz,
  followups_enviados smallint not null default 0,
  campos_coletados jsonb not null default '{}'  -- decisor, faturamento_informado, investe_anuncio, dor, teve_agencia, melhor_horario, aceita_contato
);

create table radar.mensagens (
  id           bigint generated always as identity primary key,
  conversa_id  uuid not null references radar.conversas(id),
  canal        text not null check (canal in ('whatsapp_oficial', 'whatsapp_nao_oficial', 'instagram')),
  autor        text not null check (autor in ('ia', 'lead', 'humano')),
  texto        text,
  midia_tipo   text,
  id_externo   text unique,                     -- dedup de webhook
  recebida_em  timestamptz not null default now()
);
create index mensagens_conversa_idx on radar.mensagens (conversa_id, recebida_em);

create table radar.bloqueios (
  id             bigint generated always as identity primary key,
  telefone       text,
  instagram_user text,
  lead_id        uuid references radar.leads(id),
  motivo         text not null,
  criado_em      timestamptz not null default now(),
  check (telefone is not null or instagram_user is not null)
);
create unique index bloqueios_telefone_idx on radar.bloqueios (telefone) where telefone is not null;
create unique index bloqueios_instagram_idx on radar.bloqueios (lower(instagram_user)) where instagram_user is not null;

-- ── F11/F12 · Exportações e auditoria ──────────────────────────────────────
create table radar.exportacoes (
  id          bigint generated always as identity primary key,
  tipo        text not null check (tipo in ('remarketing', 'comercial')),
  usuario_id  uuid not null references radar.usuarios(id),
  filtro      jsonb not null,
  qtd         int not null,
  destino     text,                             -- csv | webhook
  criada_em   timestamptz not null default now()
);

create table radar.historico (
  id           bigint generated always as identity primary key,
  entidade     text not null,
  entidade_id  text not null,
  acao         text not null,
  antes        jsonb,
  depois       jsonb,
  usuario_id   uuid references radar.usuarios(id),  -- null = sistema
  em           timestamptz not null default now()
);
create index historico_entidade_idx on radar.historico (entidade, entidade_id, em desc);

create table radar.custos_api (
  id           bigint generated always as identity primary key,
  fonte        text not null,
  operacao     text not null,
  pesquisa_id  uuid references radar.pesquisas(id),
  lead_id      uuid references radar.leads(id),
  qtd          int not null default 1,
  valor        numeric(12,6) not null,          -- em BRL; estimado quando o provedor não informa
  estimado     boolean not null default true,
  em           timestamptz not null default now()
);
create index custos_api_em_idx on radar.custos_api (em);
create index custos_api_pesquisa_idx on radar.custos_api (pesquisa_id);

-- ── Fila de jobs (Postgres, sem Redis; workers usam FOR UPDATE SKIP LOCKED) ─
create table radar.jobs (
  id            bigint generated always as identity primary key,
  tipo          text not null check (tipo in (
                  'minerar', 'enriquecer_cnpj', 'enriquecer_site', 'enriquecer_instagram',
                  'verificar_anuncios', 'estimar_faturamento', 'pontuar', 'enviar', 'followup',
                  'entregar_comercial')),
  pesquisa_id   uuid references radar.pesquisas(id),
  lead_id       uuid references radar.leads(id),
  payload       jsonb not null default '{}',
  status        text not null default 'pendente' check (status in ('pendente', 'rodando', 'feito', 'falhou', 'cancelado')),
  tentativas    smallint not null default 0,
  max_tentativas smallint not null default 3,
  agendado_para timestamptz not null default now(),
  travado_em    timestamptz,
  travado_por   text,
  erro          text,
  criado_em     timestamptz not null default now(),
  concluido_em  timestamptz
);
create index jobs_fila_idx on radar.jobs (tipo, agendado_para) where status = 'pendente';
create index jobs_pesquisa_idx on radar.jobs (pesquisa_id, status);

-- ── F13 · Resumo diário (agregação para o dashboard não varrer leads) ──────
create table radar.resumo_diario (
  id           bigint generated always as identity primary key,
  dia          date not null,
  pesquisa_id  uuid references radar.pesquisas(id),
  nicho        text,
  estado       char(2),
  cidade       text,
  metrica      text not null,                   -- minerados, contatados, responderam, ...
  valor        numeric(14,4) not null,
  -- recortes nulos = "todos"; NULLS NOT DISTINCT (PG15+) mantém um total único por recorte
  unique nulls not distinct (dia, metrica, pesquisa_id, nicho, estado, cidade)
);

-- ── Triggers ───────────────────────────────────────────────────────────────
create or replace function radar.tocar_atualizado_em() returns trigger
language plpgsql as $$
begin new.atualizado_em := now(); return new; end $$;

create trigger leads_atualizado_em before update on radar.leads
  for each row execute function radar.tocar_atualizado_em();

-- Toda mudança de status de lead entra no histórico (F14), venha de onde vier.
create or replace function radar.historico_status_lead() returns trigger
language plpgsql security definer set search_path = radar as $$
begin
  if new.status is distinct from old.status then
    insert into radar.historico (entidade, entidade_id, acao, antes, depois, usuario_id)
    values ('lead', new.id::text, 'status',
            jsonb_build_object('status', old.status),
            jsonb_build_object('status', new.status, 'motivo_descarte', new.motivo_descarte),
            auth.uid());
  end if;
  return new;
end $$;

create trigger leads_historico_status after update of status on radar.leads
  for each row execute function radar.historico_status_lead();

-- Lead não se apaga (F8): só descarte com motivo.
create or replace function radar.proibir_delete() returns trigger
language plpgsql as $$
begin raise exception 'lead não pode ser apagado; use descarte com motivo'; end $$;

create trigger leads_sem_delete before delete on radar.leads
  for each row execute function radar.proibir_delete();

-- ── RLS: só usuários do Radar; escrita de config só admin ─────────────────
-- Workers usam a service role (ignora RLS). O front usa a chave anon + sessão.
do $$
declare t text;
begin
  foreach t in array array['pesquisas','leads','pesquisas_leads','avaliacoes','empresas','presenca',
    'anuncios','pontuacoes','disparos','disparos_leads','conversas','mensagens','bloqueios',
    'exportacoes','historico','custos_api','jobs','resumo_diario']
  loop
    execute format('alter table radar.%I enable row level security', t);
    execute format('create policy %I on radar.%I for select to authenticated using (radar.eh_usuario())', t || '_ler', t);
  end loop;

  foreach t in array array['regras_pontuacao','abordagens','contas_envio','config','usuarios']
  loop
    execute format('alter table radar.%I enable row level security', t);
    execute format('create policy %I on radar.%I for select to authenticated using (radar.eh_usuario())', t || '_ler', t);
    execute format('create policy %I on radar.%I for all to authenticated using (radar.eh_admin()) with check (radar.eh_admin())', t || '_admin', t);
  end loop;
end $$;

-- Escritas do operador pela tela (as demais passam por rotas do servidor/workers).
create policy pesquisas_criar on radar.pesquisas for insert to authenticated
  with check (radar.eh_usuario() and usuario_id = auth.uid());
create policy leads_editar on radar.leads for update to authenticated
  using (radar.eh_usuario()) with check (radar.eh_usuario());
create policy bloqueios_criar on radar.bloqueios for insert to authenticated
  with check (radar.eh_usuario());

grant usage on schema radar to authenticated, service_role;
grant select on all tables in schema radar to authenticated;
grant insert on radar.pesquisas, radar.bloqueios to authenticated;
grant update on radar.leads to authenticated;
-- configuração: o GRANT abre, a policy *_admin restringe ao admin
grant insert, update, delete on radar.regras_pontuacao, radar.abordagens, radar.contas_envio,
  radar.config, radar.usuarios to authenticated;
grant usage, select on all sequences in schema radar to authenticated;
grant all on all tables in schema radar to service_role;
grant usage, select on all sequences in schema radar to service_role;

commit;

-- Depois de aplicar (mudança de infra, pedir aprovação à parte): incluir `radar`
-- em PGRST_DB_SCHEMAS do PostgREST da instância `supabase` para o front ler via REST.
