"""Fixtures de banco: Postgres 15 descartável com stubs do COMERCIAL + migrations 001–003.

RADAR_TEST_DSN aponta para ele (ver README). Sem a variável, os testes de banco pulam.
"""

import os

import psycopg
import pytest

DSN = os.environ.get("RADAR_TEST_DSN")
DONO = "00000000-0000-0000-0000-0000000000aa"
AGENCIA = "00000000-0000-0000-0000-00000000a001"
OUTRA_AGENCIA = "00000000-0000-0000-0000-00000000a002"
CONTA_MESTRE = "00000000-0000-0000-0000-00000000c001"

TABELAS_RADAR = """radar.jobs, radar.pesquisas_leads, radar.avaliacoes, radar.empresas, radar.presenca,
    radar.anuncios, radar.pontuacoes, radar.regras_pontuacao, radar.custos_api, radar.historico,
    radar.pesquisas, radar.leads, radar.config, radar.agencias"""


def preparar_agencias(c, teto=100.0, ativo=True):
    c.execute(f"truncate {TABELAS_RADAR} cascade")
    c.execute("insert into auth.users (id) values (%s) on conflict do nothing", (DONO,))
    c.execute("insert into public.accounts (id) values (%s) on conflict do nothing", (CONTA_MESTRE,))
    c.execute("""insert into public.profiles (user_id, account_id) values (%s, %s)
                 on conflict (user_id) do update set account_id = excluded.account_id""", (DONO, CONTA_MESTRE))
    for ag, nome in ((AGENCIA, "RCO HUB"), (OUTRA_AGENCIA, "Outra")):
        c.execute("""insert into public.agencies (id, name, owner_user_id) values (%s, %s, %s)
                     on conflict do nothing""", (ag, nome, DONO))
        c.execute("insert into radar.agencias (agency_id, ativo, teto_mensal_brl) values (%s, %s, %s)",
                  (ag, ativo, teto))


@pytest.fixture
def conn():
    if not DSN:
        pytest.skip("RADAR_TEST_DSN não definido")
    c = psycopg.connect(DSN, autocommit=True)
    preparar_agencias(c)
    yield c
    c.close()
