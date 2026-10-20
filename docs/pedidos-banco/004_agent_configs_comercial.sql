-- JÁ APLICADA EM PRODUÇÃO (instância `supabase`, COMERCIAL-RCO) em 2026-10-20 02:04:58 UTC, aprovada pelo Philip.
-- PEDIDO AO BANCO 004 (do agente crm) — configuração da IA do Radar no COMERCIAL-RCO, por AGÊNCIA (rev. 2).
-- Destino: instância `supabase` (a do COMERCIAL-RCO), schema public. Aditiva, idempotente, sem tocar em tabela existente.
-- Portado do CRM-RCO (migrations 121+122), reduzido ao que o COMERCIAL usa:
--   * chave é agency_id (FK agencies), não account_id: o Radar roda no nível da agência;
--   * só a IA do Radar (antes "pré-qualificador"): sem grupo commercial_analyzer (o analisador comercial é do SaaS),
--     sem post_sale/allowlist/category/collection_mode;
--   * colunas por agente seguem o padrão do CRM: enabled, provider, model, limites mensais, prompt.
--
-- QUEM EDITA O QUÊ (decisão do Philip; a checagem fica nas server actions, não no RLS):
--   * RCO (is_rco()): radar_agent_enabled, provider, model, limites de tokens/custo, e as chaves em agent_provider_keys;
--   * "maior papel" da agência (função única radar.pode_configurar(agency_id), definida na 003): radar_agent_prompt_override
--     (instruções de abordagem / como a IA age ao mandar a mensagem).
--   Por isso não há policy de escrita: tudo grava com service role depois da checagem na action. Leitura direta só is_rco()
--   (a tela da agência lê pelo servidor, via service role, e nunca devolve a chave).
-- Depende de (existem no COMERCIAL): agencies (027), is_rco() (050), update_updated_at_column() (001).
--
-- ── CONTRATO DA CHAVE DE API (para os workers Python lerem agent_provider_keys.api_key_encrypted) ──────────────────────
-- Algoritmo: AES-256-GCM (o mesmo de src/lib/whatsapp/encryption.ts do COMERCIAL).
-- Formato do texto salvo: "<iv_hex>:<ciphertext_hex>:<authtag_hex>"  (3 partes separadas por ':', tudo em hex minúsculo).
--   iv       = 12 bytes aleatórios (24 hex);  authtag = 16 bytes (32 hex);  ciphertext = UTF-8 da chave, cifrado, sem padding.
-- Chave mestra: variável de ambiente ENCRYPTION_KEY do app COMERCIAL (container EasyPanel crm_crm-rco), 64 caracteres hex
--   = 32 bytes. Os workers precisam receber O MESMO valor (variável própria no serviço deles, nunca no banco nem no git).
-- Decifrar em Python (pacote `cryptography`): o AESGCM espera a tag colada no fim do ciphertext:
--     from cryptography.hazmat.primitives.ciphers.aead import AESGCM
--     iv_hex, ct_hex, tag_hex = valor.split(":")
--     chave = AESGCM(bytes.fromhex(ENCRYPTION_KEY))
--     api_key = chave.decrypt(bytes.fromhex(iv_hex), bytes.fromhex(ct_hex) + bytes.fromhex(tag_hex), None).decode("utf-8")
--   (sem associated data; falha de tag lança InvalidTag = valor adulterado ou chave mestra errada.)
-- Formato legado de 2 partes (CBC) existe no código do COMERCIAL só para tokens de WhatsApp antigos; aqui nunca ocorre.

BEGIN;

CREATE TABLE IF NOT EXISTS agent_configs (
  agency_id UUID PRIMARY KEY REFERENCES agencies(id) ON DELETE CASCADE,

  -- Controlados pela RCO.
  radar_agent_enabled BOOLEAN NOT NULL DEFAULT false,
  radar_agent_provider TEXT,                       -- anthropic | openai | gemini | openrouter (validado na action)
  radar_agent_model TEXT,                          -- null = padrão do provedor
  radar_agent_monthly_token_limit INTEGER,         -- null = sem limite
  radar_agent_monthly_cost_limit_usd NUMERIC(10, 6),

  -- Controlado pelo "maior papel" da agência. null = prompt padrão versionado dos workers.
  radar_agent_prompt_override TEXT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS set_updated_at ON agent_configs;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON agent_configs
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

ALTER TABLE agent_configs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS agent_configs_select ON agent_configs;
CREATE POLICY agent_configs_select ON agent_configs FOR SELECT USING (is_rco());

-- Uma chave de API por (agência, provedor). Formato descrito acima.
CREATE TABLE IF NOT EXISTS agent_provider_keys (
  agency_id UUID NOT NULL REFERENCES agencies(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  api_key_encrypted TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (agency_id, provider)
);

DROP TRIGGER IF EXISTS set_updated_at ON agent_provider_keys;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON agent_provider_keys
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

ALTER TABLE agent_provider_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS agent_provider_keys_select ON agent_provider_keys;
CREATE POLICY agent_provider_keys_select ON agent_provider_keys FOR SELECT USING (is_rco());

COMMIT;
