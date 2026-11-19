-- RCO Radar — 008: Triagem IA (contrato: docs/triagem-ia.md)
--
-- APLICADA na instância `supabase` (vps-rco) em 18/11/2026 com aprovação do Philip, como supabase_admin, depois de ensaio com rollback das cinco (008, 009, 137, 138, 139).
-- `supabase` (vps-rco):
--   docker exec -i supabase_supabase-db-1 psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 < 008_triagem_ia.sql
--
-- O que muda:
-- * radar.conversas passa a aceitar conversa de LP (origem 'lp', sem radar.leads): lead_id nullable,
--   origem, contexto (respostas da LP) e o resultado da nota determinística (nota, nota_detalhe, decisao,
--   versao_criterios). Linhas existentes ficam origem='radar' (default) e continuam válidas no CHECK
--   (todas têm lead_id — era NOT NULL até aqui).
-- * radar.criterios_triagem: critérios por agência e origem (pesos somam 100) e os dois cortes.
--   Editados por SQL até haver tela. Vem com linha de EXEMPLO (exemplo = true) para origem 'lp' em cada
--   agência que já tem Radar, com valores provisórios até o Miguel responder.
-- Nenhum dado existente é alterado.

begin;

-- ── 1. radar.conversas ─────────────────────────────────────────────────────
alter table radar.conversas alter column lead_id drop not null;

alter table radar.conversas
  add column origem           text not null default 'radar' check (origem in ('radar', 'lp')),
  add column contexto         jsonb not null default '{}',
  add column nota             smallint check (nota between 0 and 100),
  add column nota_detalhe     jsonb,
  add column decisao          text check (decisao in ('passar', 'acompanhar', 'descartar')),
  add column versao_criterios int;

-- conversa do Radar sempre tem lead; conversa de LP nunca tem
alter table radar.conversas
  add constraint conversas_origem_lead_ck check ((origem = 'radar') = (lead_id is not null));

comment on column radar.conversas.contexto is
  'LP: {integracao_id, respondent_id, lead_source, pipeline_id, stage_id, respostas: [{pergunta, resposta}], utm: {...}}. '
  'O worker guarda em contexto.primeira o registro prévio da 1ª mensagem automática ({status, em, motivo}).';
comment on column radar.conversas.nota_detalhe is
  'Saída de radar/triagem.py: fatos usados, pontos por critério, teto possível e cortes da versão aplicada.';

-- o despachante de LP procura conversas ainda sem 1ª mensagem
create index conversas_lp_pendentes_idx on radar.conversas (agency_id)
  where origem = 'lp' and estado = 'ia' and iniciada_em is null;

-- ── 2. radar.criterios_triagem ─────────────────────────────────────────────
create table radar.criterios_triagem (
  id              bigint generated always as identity primary key,
  agency_id       uuid not null references public.agencies(id),
  origem          text not null check (origem in ('radar', 'lp')),
  versao          int not null default 1,
  -- [{campo, peso, regra}]; regra = {"tipo": "em", "valores": [...]} | {"tipo": "preenchido"}
  --                                | {"tipo": "contem", "valores": [...]} (campo "resposta:<trecho da pergunta da LP>")
  criterios       jsonb not null check (jsonb_typeof(criterios) = 'array' and jsonb_array_length(criterios) > 0),
  corte_passar    smallint not null check (corte_passar between 0 and 100),
  corte_descartar smallint not null check (corte_descartar between 0 and 100),
  ativo           boolean not null default true,
  exemplo         boolean not null default false,   -- true = valores provisórios, ainda não definidos pela agência
  criado_em       timestamptz not null default now(),
  check (corte_descartar <= corte_passar),
  unique (agency_id, origem, versao)
);
-- uma linha ativa por agência e origem
create unique index criterios_triagem_ativo_uk on radar.criterios_triagem (agency_id, origem) where ativo;

create trigger criterios_triagem_trava_agencia before update on radar.criterios_triagem
  for each row execute function radar.travar_agencia();

alter table radar.criterios_triagem enable row level security;
create policy criterios_triagem_ler on radar.criterios_triagem for select to authenticated
  using (agency_id in (select radar.minhas_agencias()));
grant select on radar.criterios_triagem to authenticated;
grant all on radar.criterios_triagem to service_role;
grant select on radar.criterios_triagem to radar_worker;

-- EXEMPLO (até o Miguel definir): pesos somam 100; fatos extraídos pela IA (radar/ia.py: Fatos).
insert into radar.criterios_triagem (agency_id, origem, versao, criterios, corte_passar, corte_descartar, exemplo)
select a.agency_id, 'lp', 1, '[
  {"campo": "decisor",           "peso": 20, "regra": {"tipo": "em", "valores": ["sim"]}},
  {"campo": "faturamento_faixa", "peso": 25, "regra": {"tipo": "em", "valores": ["30k_80k", "80k_200k", "200k_500k", "acima_500k"]}},
  {"campo": "investe_anuncio",   "peso": 15, "regra": {"tipo": "em", "valores": ["sim"]}},
  {"campo": "dor_clara",         "peso": 20, "regra": {"tipo": "em", "valores": ["sim"]}},
  {"campo": "urgencia",          "peso": 10, "regra": {"tipo": "em", "valores": ["alta", "media"]}},
  {"campo": "aceita_contato",    "peso": 10, "regra": {"tipo": "em", "valores": ["sim"]}}
]'::jsonb, 70, 30, true
from radar.agencias a;

commit;

-- Depois de aplicar: conferir por leitura (\d radar.conversas, \d radar.criterios_triagem,
-- select agency_id, origem, exemplo from radar.criterios_triagem) e anotar em status/banco.md.
