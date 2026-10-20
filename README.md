# RCO Radar

Minerador e qualificador de leads de negócios locais — uso interno da RCO Hub.
Levantamento: `docs/levantamento-v1.0.pdf` (17/10/2026).

## Estrutura

| Pasta | O quê | Estado |
|---|---|---|
| `supabase/migrations/` | Schema `radar` na instância `supabase` (a mesma do COMERCIAL-RCO) | 001 escrita e validada em Postgres 15 local — **não aplicada** |
| `workers/` | Python 3.11: fila, adaptadores de fontes, regras (pontuação, CNPJ, trava de envio) | esqueleto + regras puras testadas |
| `web/` | Next.js 16 + React 19 + `@supabase/ssr` (mesma base do COMERCIAL-RCO) | aguarda wireframes aprovados |
| `docs/fase0/` | Escopo técnico da Fase 0 (PDF para o Luis) e rascunhos das 4 telas | entregue 20/10 |

## Arquitetura

```
Next.js (tela) ──> Supabase (Auth + PostgREST + Realtime) ──> schema radar
                                                                 │ radar.jobs (FOR UPDATE SKIP LOCKED)
                                         workers Python ─────────┘
                                         ├─ fontes/: Maps · CNPJ · Instagram · Meta Ads · site
                                         └─ canais/: Evolution Go · WhatsApp Cloud API · IA
```

Regras que o código garante (não só o prompt): nada é enviado sem disparo confirmado e
registro prévio em `disparos_leads` (`gate.py`); lead não se apaga, só se descarta com motivo
(trigger); CNPJ com confiança < 50 não é assumido (CHECK); toda mudança de status vai para
`historico` (trigger).

## Testes

```bash
cd workers && python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
# fila contra Postgres real (opcional):
docker run -d --name radar-pg -e POSTGRES_PASSWORD=x -p 54329:5432 postgres:15
# aplicar supabase/tests/stub_supabase.sql e a 001 num banco "t", depois:
RADAR_TEST_DSN=postgresql://postgres:x@localhost:54329/t .venv/bin/pytest -q
```
