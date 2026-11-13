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

---

## v2 — Processo comercial da Maria (Método Opala), decidido pelo Philip em 13/11/2026

Fonte: "Definições do processo comercial (Método Opala)" da Maria Ricarte + documento do Miguel.
Substitui a nota ponderada 0-100 (a coluna `nota` pode ficar, mas a decisão sai dos portões).

### Decisões do Philip (13/11)
- Decisão por **portões + 4 saídas** (não mais nota/cortes).
- Cadência: **5 toques + esteira** (não 10).
- **A IA oferece 2 horários** a quem passa direto, lidos do **Google Calendar do SDR**.
- A IA **não diz preço nem o piso do Opala**: "depende do caso, o consultor mostra na conversa".

### Fatos que a IA extrai (só fatos; o código decide)
`segmento`, `tempo_mercado`, `como_atrai_clientes`, `investe_anuncio` (sim/nao/indefinido) + `valor_anuncio`
(texto livre), `dor` (palavras do lead), `necessidade_clara` (sim/nao/indefinido), `decisor`
(dono_socio | participa | nao_decide | indefinido), `contato_decisor`, `prazo_inicio`
(ate_30d | 30_60d | mais_60d | so_pesquisando | indefinido), `faixa_faturamento`
(ate_30k | 30_60k | 60_100k | acima_100k | indefinido), `faturamento_valor` (número, se disser),
`cidade`, `experiencia_agencia` (texto), `ve_marketing_como_despesa` (sim/nao/indefinido),
`pede_algo_fora` (sim/nao/indefinido), `objecoes` (lista curta).
Lead de LP: fatos já respondidos no formulário entram como conhecidos e a IA NÃO repete a pergunta;
usa-os para aprofundar ("Vi que seu faturamento está na faixa dos R$ 40 mil. Há quanto tempo...").
Faixa 30_60k: a IA faz UMA pergunta de refino ("mais perto de 30 ou de 60?") para separar 50-60k.

### Roteiro (uma pergunta por vez, curta, espelhando as palavras do lead; não é formulário)
1 S situação · 2 S marketing hoje · 3 N necessidade · 4 A autoridade · 5 T timing · 6 B faixa de faturamento
(por último, em faixas). Extra de mentalidade: experiência com agência. Cidade cedo (define o produto).
Textos de referência no documento da Maria; ficam em config por agência.

### Portões (config por agência em radar.config 'triagem_regras', valores padrão abaixo)
- Faturamento: >= 60k passa; 50-60k = alerta; < 50k falha por porte.
- Decisor: dono_socio passa; participa/indefinido no fim = alerta; nao_decide = pede contato do decisor
  ou conversa com os dois (não descarta).
- Cidade: Teresina = Opala; Timon e próximas (lista em config) = alerta "avaliação comercial";
  outras = alerta "ticket diferenciado". Gestão de tráfego isolada: nacional.
- Necessidade clara: sim passa; não = falha.
- Timing: ate_30d/30_60d passa; mais_60d/so_pesquisando = esteira.
- Telefone válido: conversa no WhatsApp ativa = válido.
- Eliminatórios: ve_marketing_como_despesa=sim, sem orçamento nem intenção, pede_algo_fora=sim.

### Saídas
- **passa_direto**: todos os portões ok e timing <= 60 dias → IA oferece 2 horários (Google do SDR),
  lead escolhe, cria evento + reunião no COMERCIAL, passa ao SDR. Sem Google conectado/sem horário →
  passa ao SDR sem oferecer.
- **passa_alerta**: exatamente um portão em alerta → passa ao SDR com o alerta no resumo; NÃO oferece horário.
- **esteira**: bom perfil (sem eliminatório, porte ok) mas timing > 60 d ou "só pesquisando".
- **desqualifica**: eliminatório. Se falha só por cidade/porte do Opala → **redireciona** (candidato a
  gestão de tráfego isolada; porte mínimo dela A DEFINIR → até lá passa ao SDR com alerta "tráfego isolado").
A decisão só sai com os fatos necessários conhecidos ou com um eliminatório; antes disso, a conversa segue.

### Cadência sem resposta (só seg-sex 9h-18h no fuso da agência; fora disso, empurra para a próxima janela)
Toque 1 imediato (até 5 min se veio de formulário) · 2: +2 h, retoma sem cobrar · 3: +1 dia, pergunta fácil
(sim/não ou faixa) · 4: +3 dias, valor ligado ao segmento · 5: +7 dias, fechamento gentil, porta aberta.
Depois: esteira (se bom perfil já conhecido) ou encerra.

### Esteira de acompanhamento
1 mensagem a cada 30 dias, por 3 meses, com valor (caso, ideia, novidade), nunca "ainda tem interesse?".
Se responder, a IA retoma a qualificação de onde parou. 90 dias sem resposta → "Sem resposta", para.

### O que a IA não trata (passa ao humano ou responde que o consultor trata)
Preço/piso, descontos, condições especiais, contratos, promessa de resultado, comparação com concorrentes.
Se perguntada se é robô: não nega ser IA (limite já registrado).

### Resumo ao SDR (nota interna + observação do negócio)
Nome, empresa, telefone, segmento, cidade; faixa/valor de faturamento e sinais de consistência;
investimento atual em anúncio; estrutura de marketing e conteúdo; dor/objetivo nas palavras do lead;
decisor; quando quer começar; experiência com agência; objeções; **classificação + critério em que passou
ou ficou em alerta**; link da conversa; horário escolhido (se houve).

### Agenda (Google Calendar do SDR) — COMERCIAL
- Conexão OAuth do Google por usuário SDR (escopos freebusy + events), token cifrado no banco.
- Rota interna `POST /api/internal/radar/horarios` {conversa/contato, agency_id} → escolhe o SDR pela
  mesma distribuição de hoje, lê freebusy e devolve 2 opções (padrão: 30 min, seg-sex 9-18, antecedência
  mínima 2 h, dias/turnos diferentes) + `sdr_id`.
- Rota interna `POST /api/internal/radar/agendar` {opção escolhida, sdr_id, ...} → revalida o horário,
  cria o evento (com Google Meet), cria a reunião em `lead_calls`, move o negócio para "Reunião agendada".
- Tela de conectar o Google: decisão de UI do Philip (mostrar antes de publicar).
- Credenciais OAuth do Google Cloud: o Philip fornece.

### Medição (fase 2, depende de tela → aprovação do Philip)
SDR avalia cada lead (bom/médio/ruim + motivo); amostra semanal dos descartados nas 4 primeiras semanas;
indicadores: % passados que viram reunião realizada, comparecimento, conversão.
