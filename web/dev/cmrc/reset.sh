#!/bin/sh
# Recria o banco local "cmrc": stub do COMERCIAL (modelo real: Plataforma RCO x agência RCO HUB) + schema radar (001+003)
# + pedido 004 (agent_configs) (sobe VAZIO, sem dados de exemplo). Recarrega o schema do PostgREST.
set -e
export DOCKER_HOST=unix:///Users/philip/.colima/default/docker.sock
cd "$(dirname "$0")"
PSQL="docker exec -i radar-web-pg psql -U postgres -d cmrc -v ON_ERROR_STOP=1 -q"
docker exec radar-web-pg psql -U postgres -qc "drop database if exists cmrc with (force)" -c "create database cmrc" 2>&1 | grep -v NOTICE || true
$PSQL < 10_stub_comercial.sql
./20_radar_seed.sh | tail -1
$PSQL < ../../../docs/pedidos-banco/004_agent_configs_comercial.sql 2>&1 | grep -v NOTICE || true
$PSQL < ../../../supabase/migrations/005_conversa_no_inbox.sql
$PSQL <<SQL
grant all on agent_configs, agent_provider_keys to service_role, authenticated;
insert into agent_configs (agency_id, radar_agent_enabled, radar_agent_provider, radar_agent_model, radar_agent_monthly_cost_limit_usd)
values ('aaaaaaaa-1111-0000-0000-000000000001', true, 'anthropic', 'claude-haiku-4-5-20251001', 30);
SQL
docker kill -s SIGUSR1 radar-cmrc-postgrest >/dev/null
echo "cmrc pronto"
