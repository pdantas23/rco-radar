"""Robô que consome radar.jobs. Rodar: `python -m radar.worker` (RADAR_DATABASE_URL no ambiente).

Vários processos podem rodar ao mesmo tempo (a fila usa SKIP LOCKED).
"""

import asyncio
import logging
import os
import socket
import time

import httpx
import psycopg

from radar import etapas, fila

log = logging.getLogger("radar.worker")
NOMINATIM = "https://nominatim.openstreetmap.org/search"


def geocodificador(conn):
    """Cidade -> (lat, lng) pelo Nominatim, com cache permanente em radar.config (1 consulta por cidade)."""
    async def geocodificar(cidade: str, uf: str, agency_id: str):
        chave = f"geo:{cidade.strip().lower()}|{uf.lower()}"
        row = conn.execute("select valor from radar.config where agency_id = %s and chave = %s",
                           (agency_id, chave)).fetchone()
        if row:
            return row[0]["lat"], row[0]["lng"]
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "RCO-Radar/0.1 (uso interno RCO Hub)"}) as c:
            r = await c.get(NOMINATIM, params={"city": cidade, "state": uf, "country": "Brasil",
                                               "format": "json", "limit": 1})
        dados = r.json() if r.status_code == 200 else []
        if not dados:
            return None
        lat, lng = float(dados[0]["lat"]), float(dados[0]["lon"])
        conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, %s, %s::jsonb)
                        on conflict (agency_id, chave) do nothing""",
                     (agency_id, chave, f'{{"lat": {lat}, "lng": {lng}}}'))
        return lat, lng
    return geocodificar


def fontes_reais(conn) -> etapas.Fontes:
    from radar.fontes.anuncios_apify import AnunciosApify
    from radar.fontes.cnpj import CnpjCasaDosDados
    from radar.fontes.instagram_apify import InstagramApify
    from radar.fontes.maps_apify import MapsApify
    from radar.fontes.maps_gosom import FonteMapsComReserva, MapsGosom
    # Maps: gosom no nosso servidor (grátis); Apify assume se o gosom falhar
    apify = MapsApify() if os.environ.get("APIFY_TOKEN") else None
    proprio = FonteMapsComReserva(MapsGosom(), apify)        # Apify só entra se o gosom falhar e couber no teto
    mapas = {"gosom_maps": proprio}
    if apify:
        mapas["apify_maps"] = apify                          # o usuário escolheu a API (paga)
    return etapas.Fontes(proprio, CnpjCasaDosDados(), InstagramApify(), AnunciosApify(), geocodificador(conn),
                         mapas=mapas)


async def processar_um(conn: psycopg.Connection, f: etapas.Fontes, nome: str,
                       tipos: list[str] | None = None) -> bool:
    """Pega e executa um job. Devolve False se a fila estava vazia."""
    job = fila.pegar(conn, tipos or list(etapas.HANDLERS), nome)
    if job is None:
        return False
    try:
        with conn.transaction():
            await etapas.HANDLERS[job.tipo](conn, job, f)
            fila.concluir(conn, job)
    except etapas.RadarDesligado as e:
        fila.cancelar(conn, job, str(e))
        if job.tipo == "minerar" and job.pesquisa_id:
            conn.execute("update radar.pesquisas set status = 'cancelada' where id = %s", (job.pesquisa_id,))
    except Exception as e:                      # noqa: BLE001 — qualquer falha vira retentativa
        log.warning("job %s (%s) falhou: %s", job.id, job.tipo, e)
        vai_tentar = fila.falhar(conn, job, f"{type(e).__name__}: {e}")
        if not vai_tentar and job.lead_id and job.tipo in etapas.AO_FALHAR:
            conn.execute(etapas.AO_FALHAR[job.tipo], (job.lead_id,))
        if not vai_tentar and job.tipo == "minerar" and job.pesquisa_id:
            conn.execute("update radar.pesquisas set status = 'falhou' where id = %s", (job.pesquisa_id,))
    if job.lead_id and job.tipo in etapas.ENRIQUECIMENTO:
        etapas.talvez_finalizar(conn, job.lead_id)
    if job.pesquisa_id:
        _talvez_concluir_pesquisa(conn, job.pesquisa_id)
    return True


def _talvez_concluir_pesquisa(conn, pesquisa_id) -> None:
    conn.execute(
        """update radar.pesquisas set status = 'concluida'
           where id = %s and status = 'enriquecendo'
             and not exists (select 1 from radar.jobs where pesquisa_id = %s and status in ('pendente','rodando'))""",
        (pesquisa_id, pesquisa_id))


async def laco(dsn: str, espera: float = 2.0) -> None:
    nome = f"{socket.gethostname()}:{os.getpid()}"
    conn = psycopg.connect(dsn, autocommit=True)
    f = fontes_reais(conn)
    log.info("worker %s no ar", nome)
    while True:
        if not await processar_um(conn, f, nome):
            time.sleep(espera)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(laco(os.environ["RADAR_DATABASE_URL"]))
