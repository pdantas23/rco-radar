-- JÁ APLICADA EM PRODUÇÃO (instância supabase interna) em 2026-10-20 01:26:12 UTC, aprovada pelo Philip.
-- RCO Radar — 002: guardar todos os dados que as fontes devolvem (decisão do Luis, 20/10/2026)
--
-- Só ADD COLUMN, sem DEFAULT que reescreva a tabela e sem CHECK que valide linhas
-- existentes. Não toca em nada fora do schema `radar`.
-- Dados de pessoa física (CPF de sócio, nome de quem avaliou) são removidos no
-- worker antes de gravar; estas colunas nunca recebem CPF.

begin;

-- Resposta completa do Google Maps (faixa de preço, fotos, distribuição das notas, etc.)
alter table radar.leads add column if not exists dados_maps jsonb;
-- E-mail do negócio, de onde veio (site, Receita) — usado no CSV de remarketing (F11)
alter table radar.leads add column if not exists email text;
alter table radar.leads add column if not exists email_origem text;

-- Resposta completa do Instagram (categoria, nome completo, verificado, destaques…)
alter table radar.presenca add column if not exists dados_instagram jsonb;
-- Outras redes achadas no site: {"facebook": "...", "tiktok": "...", "youtube": "...", "linkedin": "..."}
alter table radar.presenca add column if not exists redes jsonb;
-- E-mails achados no site
alter table radar.presenca add column if not exists site_emails text[];

-- Resposta completa da Receita (Casa dos Dados + BrasilAPI), sem CPF
alter table radar.empresas add column if not exists dados_receita jsonb;

commit;
