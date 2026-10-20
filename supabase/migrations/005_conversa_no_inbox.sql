-- RCO Radar — 005: conversa do Radar ligada à conversa do inbox do COMERCIAL (Fase 3)
--
-- JÁ APLICADA EM PRODUÇÃO (instância `supabase`) em 2026-10-20 23:07:33 UTC, aprovada pelo Philip. Desenho aprovado pelo Philip em 20/10/2026: as mensagens vivem só no inbox
-- (public.messages); o Radar guarda o estado da conversa (ia | humano | encerrada), os campos
-- coletados e o ritmo dos acompanhamentos. Só ADD COLUMN / CREATE INDEX no schema radar; nenhuma
-- tabela do COMERCIAL é alterada (só passam a ser referenciadas).

begin;

alter table radar.conversas
  add column if not exists conversation_id uuid references public.conversations(id) on delete set null,
  add column if not exists contact_id      uuid references public.contacts(id)      on delete set null,
  add column if not exists whatsapp_config_id uuid references public.whatsapp_config(id) on delete set null,
  add column if not exists iniciada_em     timestamptz,           -- 1ª mensagem enviada
  add column if not exists lida_ate        timestamptz,           -- última mensagem do lead já tratada pela IA
  add column if not exists proximo_followup_em timestamptz,       -- 24h / 72h sem resposta
  add column if not exists motivo_fim      text;                  -- qualificado | sem_interesse | pediu_humano | sem_resposta | humano_assumiu

-- uma conversa do inbox pertence a no máximo um lead do Radar
create unique index if not exists conversas_inbox_uk on radar.conversas (conversation_id) where conversation_id is not null;
-- o robô procura as conversas em que a IA está falando
create index if not exists conversas_ia_idx on radar.conversas (agency_id, estado) where estado = 'ia';

-- o lead entregue aponta para o negócio criado no funil (já existe leads.deal_id/contact_id da 003)

commit;
