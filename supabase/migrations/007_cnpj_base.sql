-- RCO Radar — 007: base própria de CNPJ (Dados Abertos da Receita Federal)
--
-- NÃO APLICADA. Aplicar só com aprovação do Philip, na instância `supabase` (vps-rco):
--   docker exec -i supabase_supabase-db-1 psql -U supabase_admin -d postgres -v ON_ERROR_STOP=1 < 007_cnpj_base.sql
--
-- Só estabelecimentos ATIVOS. Colunas mínimas: sem sócio, sem CPF, sem telefone/e-mail. O endereço fica para o cnpj_match.
-- O CPF que a Receita coloca no fim da razão social de MEI/empresário individual é removido pelo importador.
-- Sem agency_id de propósito: é dado público de referência, igual para todas as agências.
-- cnpj é char(14) de texto: a Receita emite CNPJ alfanumérico desde 07/2026.

begin;

-- ainda não instalada nesta instância; no schema radar para não mexer em nada fora dele
create extension if not exists pg_trgm with schema radar;

create table radar.cnpj_base (
  cnpj           char(14) primary key,
  razao_social   text not null,
  nome_fantasia  text not null default '',
  uf             char(2) not null,
  municipio      text not null,        -- nome normalizado (minúsculo, sem acento), comparável ao do Maps
  cnae_principal char(7),
  porte          text check (porte in ('ME', 'EPP', 'DEMAIS')),   -- null = não informado
  logradouro     text not null default '',   -- tipo + nome, como o cnpj_match compara
  numero         text not null default '',
  cep            char(8),
  nome_busca     text not null         -- fantasia + razão normalizadas (sem acento, sem ruído); só para busca
);

-- filtro por cidade (cidades homônimas: sempre com a UF)
create index cnpj_base_cidade_idx on radar.cnpj_base (uf, municipio);
-- busca por nome: LIKE '%termo%' e similaridade
create index cnpj_base_nome_trgm_idx on radar.cnpj_base using gin (nome_busca radar.gin_trgm_ops);

-- Sem policy: nada chega por PostgREST. Só os robôs (radar_worker tem BYPASSRLS).
alter table radar.cnpj_base enable row level security;
revoke all on radar.cnpj_base from anon, authenticated;
grant select, insert, update, delete on radar.cnpj_base to radar_worker;

commit;
