"""Fila contra Postgres real com a migration aplicada.

Precisa de RADAR_TEST_DSN apontando para um Postgres 15 descartável com o stub
do Supabase (roles + auth.users) e a 001 aplicada. Sem a variável, pula.
"""

import os
import threading

import psycopg
import pytest

from radar import fila

DSN = os.environ.get("RADAR_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="RADAR_TEST_DSN não definido")


@pytest.fixture
def conn():
    c = psycopg.connect(DSN, autocommit=True)
    c.execute("delete from radar.jobs")
    yield c
    c.execute("delete from radar.jobs")
    c.close()


def test_pega_conclui(conn):
    jid = fila.enfileirar(conn, "pontuar", payload={"x": 1})
    job = fila.pegar(conn, ["pontuar"], "w1")
    assert job.id == jid and job.payload == {"x": 1} and job.tentativas == 1
    assert fila.pegar(conn, ["pontuar"], "w2") is None      # já reservado
    fila.concluir(conn, job)
    assert conn.execute("select status from radar.jobs where id=%s", (jid,)).fetchone()[0] == "feito"


def test_tipo_filtrado(conn):
    fila.enfileirar(conn, "enviar")
    assert fila.pegar(conn, ["minerar"], "w1") is None


def test_falha_reagenda_e_desiste(conn):
    jid = fila.enfileirar(conn, "verificar_anuncios")
    for tentativa in (1, 2):
        job = fila.pegar(conn, ["verificar_anuncios"], "w1")
        assert fila.falhar(conn, job, f"bloqueado {tentativa}") is True
        conn.execute("update radar.jobs set agendado_para = now() where id=%s", (jid,))  # pula o backoff
    job = fila.pegar(conn, ["verificar_anuncios"], "w1")
    assert fila.falhar(conn, job, "bloqueado 3") is False
    st, erro = conn.execute("select status, erro from radar.jobs where id=%s", (jid,)).fetchone()
    assert st == "falhou" and erro == "bloqueado 3"


def test_worker_morto_devolve_job(conn):
    jid = fila.enfileirar(conn, "minerar")
    fila.pegar(conn, ["minerar"], "w-morto")
    conn.execute("update radar.jobs set travado_em = now() - interval '1 hour' where id=%s", (jid,))
    job = fila.pegar(conn, ["minerar"], "w-vivo")
    assert job and job.id == jid and job.tentativas == 2


def test_concorrencia_cada_job_uma_vez(conn):
    """8 workers em paralelo disputando 200 jobs: nenhum job pego duas vezes."""
    for _ in range(200):
        fila.enfileirar(conn, "enriquecer_site")
    pegos: list[int] = []
    trava = threading.Lock()

    def worker(nome):
        c = psycopg.connect(DSN, autocommit=True)
        while (job := fila.pegar(c, ["enriquecer_site"], nome)) is not None:
            with trava:
                pegos.append(job.id)
            fila.concluir(c, job)
        c.close()

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(pegos) == 200 and len(set(pegos)) == 200
