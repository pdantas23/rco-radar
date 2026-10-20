"""Fila de jobs no próprio Postgres (radar.jobs), sem Redis.

Vários workers pegam jobs ao mesmo tempo com FOR UPDATE SKIP LOCKED: cada job
vai para exatamente um worker. Job travado há mais que `ttl` (worker morreu)
volta para a fila. Falha não trava o lead: depois de max_tentativas o job vira
'falhou' e o worker marca o campo como 'nao_verificado'.
"""

import json
from dataclasses import dataclass
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row

BACKOFF_BASE = timedelta(minutes=1)


@dataclass
class Job:
    id: int
    tipo: str
    agency_id: str
    pesquisa_id: str | None
    lead_id: str | None
    payload: dict
    tentativas: int
    max_tentativas: int


def enfileirar(conn: psycopg.Connection, tipo: str, *, agency_id, pesquisa_id=None, lead_id=None,
               payload: dict | None = None, agendado_para=None) -> int:
    row = conn.execute(
        """insert into radar.jobs (agency_id, tipo, pesquisa_id, lead_id, payload, agendado_para)
           values (%s, %s, %s, %s, %s, coalesce(%s, now())) returning id""",
        (agency_id, tipo, pesquisa_id, lead_id, json.dumps(payload or {}), agendado_para),
    ).fetchone()
    return row[0]


def pegar(conn: psycopg.Connection, tipos: list[str], worker: str,
          ttl: timedelta = timedelta(minutes=10)) -> Job | None:
    """Reserva o próximo job pronto de um dos tipos. Commit imediato da reserva."""
    with conn.transaction():
        # devolve à fila jobs de worker que morreu
        conn.execute(
            """update radar.jobs set status = 'pendente', travado_em = null, travado_por = null
               where status = 'rodando' and travado_em < now() - %s""", (ttl,))
        row = conn.cursor(row_factory=dict_row).execute(
            """update radar.jobs set status = 'rodando', travado_em = now(), travado_por = %s,
                      tentativas = tentativas + 1
               where id = (
                 select id from radar.jobs
                 where status = 'pendente' and tipo = any(%s) and agendado_para <= now()
                 order by agendado_para, id
                 for update skip locked
                 limit 1)
               returning id, tipo, agency_id, pesquisa_id, lead_id, payload, tentativas, max_tentativas""",
            (worker, tipos),
        ).fetchone()
    if not row:
        return None
    row["agency_id"] = str(row["agency_id"])
    row["pesquisa_id"] = str(row["pesquisa_id"]) if row["pesquisa_id"] else None
    row["lead_id"] = str(row["lead_id"]) if row["lead_id"] else None
    return Job(**row)


def cancelar(conn: psycopg.Connection, job: Job, motivo: str) -> None:
    conn.execute("update radar.jobs set status = 'cancelado', erro = %s, concluido_em = now() where id = %s",
                 (motivo[:2000], job.id))


def concluir(conn: psycopg.Connection, job: Job) -> None:
    conn.execute("update radar.jobs set status = 'feito', concluido_em = now(), erro = null where id = %s",
                 (job.id,))


def falhar(conn: psycopg.Connection, job: Job, erro: str, definitiva: bool = False) -> bool:
    """Reagenda com backoff exponencial. Devolve True se ainda vai tentar de novo.

    definitiva=True: não tenta de novo (ex.: o servidor próprio do Maps falhou e a decisão
    de ir para a API é do usuário, na tela).
    """
    if definitiva or job.tentativas >= job.max_tentativas:
        conn.execute("update radar.jobs set status = 'falhou', erro = %s, concluido_em = now() where id = %s",
                     (erro[:2000], job.id))
        return False
    atraso = BACKOFF_BASE * (2 ** (job.tentativas - 1))
    conn.execute(
        """update radar.jobs set status = 'pendente', erro = %s, travado_em = null, travado_por = null,
                  agendado_para = now() + %s where id = %s""",
        (erro[:2000], atraso, job.id))
    return True


def progresso(conn: psycopg.Connection, pesquisa_id: str) -> dict[str, int]:
    """Contagem por status dos jobs de uma pesquisa (barra de progresso na tela)."""
    rows = conn.execute(
        "select status, count(*) from radar.jobs where pesquisa_id = %s group by status", (pesquisa_id,)
    ).fetchall()
    return {s: n for s, n in rows}
