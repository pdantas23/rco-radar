# Ambiente local de desenvolvimento (nunca produção)

Docker (colima: `export DOCKER_HOST=unix:///Users/philip/.colima/default/docker.sock`):
- `radar-web-pg` (postgres:15, porta 54330): bancos `postgres` (protótipo standalone) e `cmrc` (stub do COMERCIAL + radar 001+003 + seed).
- `radar-cmrc-postgrest` (54342, schemas public,radar) e `radar-web-postgrest` (54332).
- Gateway de auth fake: `node dev/fake-supabase.mjs` (login por e-mail com senha `radar-dev`: luis@rcohub.dev = RCO/dono, ana@rcohub.dev = vendedora). `PORT=54341 REST_URL=http://localhost:54342` para o `cmrc`.
- `dev/cmrc/reset.sh` recria o banco `cmrc` do zero.
- O COMERCIAL roda de uma CÓPIA no scratchpad, sem .env de produção: `.env.local` aponta para 54341, `CSP_REPORT_ONLY=true`, `AUTOMATION_CRON_SECRET=` vazio.
