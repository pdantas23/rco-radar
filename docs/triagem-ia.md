# Triagem IA — contrato entre COMERCIAL e worker do Radar

Branch `feat/triagem-ia` nos dois repos. Nada é publicado em partes: tudo vai junto,
depois de testado local (decisão do Philip, 11/11/2026). Migrations ficam só como
arquivo até aprovação explícita; nunca aplicar em produção.

## Objetivo
Leads NOVOS das landing pages (integrações Respondi marcadas para triagem) e do Radar
passam por uma IA que conversa pelo WhatsApp, junta fatos, recebe uma NOTA
determinística por critérios e só vai ao funil SDR quem passa do corte. Leads antigos
não entram.

## Decisões
- O motor é o worker Python do Radar (`workers/radar/`), generalizado: uma conversa de
  triagem pode vir do Radar (`origem='radar'`, com `radar.leads`) ou de LP
  (`origem='lp'`, sem `radar.leads`).
- Do analisador-comercial só se copia código (não se usa em produção): a ideia de nota
  de `qualification.classificar` (IA devolve fatos; código calcula a nota) e o provider
  OpenRouter (`providers/openrouter.py`).
- A IA nunca decide sozinha "vale a pena": a decisão sai da nota e dos cortes.
- Lead de LP recebe a 1ª mensagem automaticamente (sem "Aprovar para contato"), dentro
  da janela de horário e dos limites da agência, e passando pela trava de envio
  (registro prévio do disparo, como o Radar já faz).
- Se a agência não tiver Radar ativo ou conexão de WhatsApp configurada, o webhook da LP
  segue o fluxo de hoje (negócio direto em SDR / Novo Lead) e registra o motivo no log.
- Sem tela nova nesta etapa (decisão de UI é do Philip). A integração é marcada para
  triagem por coluna; os critérios ficam em tabela, editados por SQL até haver tela.

## Banco (arquivos de migration, NÃO aplicar)
COMERCIAL `supabase/migrations/137_respondi_triagem_ia.sql`:
- `public.respondi_integrations.triagem_ia boolean not null default false`.

Radar `supabase/migrations/008_triagem_ia.sql`:
- `radar.conversas`:
  - `lead_id` passa a aceitar null;
  - `origem text not null default 'radar' check (origem in ('radar','lp'))`;
  - `check ((origem = 'radar') = (lead_id is not null))`;
  - `contexto jsonb not null default '{}'` — para LP: `{integracao_id, lead_source,
    pipeline_id, stage_id, respostas: [{pergunta, resposta}], utm: {...}}`;
  - `nota smallint check (nota between 0 and 100)`, `nota_detalhe jsonb`,
    `decisao text check (decisao in ('passar','acompanhar','descartar'))`,
    `versao_criterios int`.
- `radar.criterios_triagem` (uma linha ativa por agência e origem):
  `id, agency_id, origem ('radar'|'lp'), versao, criterios jsonb, corte_passar smallint,
  corte_descartar smallint, ativo boolean, criado_em`.
  `criterios` = lista de `{campo, peso, regra}`; pesos somam 100. Valores de EXEMPLO até o
  Miguel responder (marcar como exemplo).

## Fluxo LP
1. Webhook `/api/webhooks/leads/respondi/[token]` com `triagem_ia = true`: cria/acha o
   contato e grava as respostas como hoje, NÃO cria negócio; cria `radar.conversas`
   (`origem='lp'`, `estado='ia'`, `contact_id`, `agency_id` da conta, `contexto`).
   Idempotente pelo `respondent_id` (já existe).
2. Worker despacha a 1ª mensagem (template por origem, texto em config; exemplo até o
   Miguel definir), conversa, extrai fatos, recalcula a nota a cada resposta.
3. Decisão: `passar` → rota `/api/internal/radar/passar` cria o negócio no
   `pipeline_id/stage_id` do `contexto` (o mesmo lugar onde cairia hoje) com resumo nas
   notas e distribuição como hoje; `acompanhar` → follow-ups existentes (24 h / +48 h);
   `descartar` → encerra com motivo e etiqueta "Fora do perfil".
4. Humano responde → IA para (mecanismo atual).

## Correções incluídas
- `limite_custo_usd` do agente passa a ser aplicado (hoje é lido e ignorado).
- Provider OpenRouter no worker, escolhido por `agent_configs` (Anthropic continua).

## Testes
Sem chamada paga: provider falso, banco local/stub. Cobrir: webhook com e sem triagem,
fallback sem Radar, idempotência, 1ª mensagem automática respeitando janela e trava,
nota e cortes, passagem para o funil/etapa da integração, teto de custo, humano assume.
Prova por mutação nos pontos centrais (neutralizar a correção → teste falha).
