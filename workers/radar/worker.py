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

from radar import custo_zero, etapas, fila

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


def fonte_cnpj(conn):
    """RADAR_FONTE_CNPJ: 'base_local' (padrão, custo zero) ou 'casadosdados' (paga)."""
    from radar.fontes.cnpj import CnpjCasaDosDados
    from radar.fontes.cnpj_local import CnpjBaseLocal
    escolha = os.environ.get("RADAR_FONTE_CNPJ", "base_local").strip().lower()
    if escolha == "base_local":
        return CnpjBaseLocal(conn)
    if escolha == "casadosdados":
        return CnpjCasaDosDados()
    raise ValueError(f"RADAR_FONTE_CNPJ inválida: {escolha!r} (use base_local ou casadosdados)")


def fontes_custo_zero(conn) -> etapas.Fontes:
    """Modo só dados abertos: nenhum adaptador pago é criado, nem com token no ambiente."""
    from radar.fontes.cnpj_local import CnpjBaseLocal
    from radar.fontes.maps_gosom import MapsGosom
    if os.environ.get("RADAR_FONTE_CNPJ", "base_local").strip().lower() != "base_local":
        raise ValueError("RADAR_SO_DADOS_ABERTOS=1 exige RADAR_FONTE_CNPJ=base_local")
    proprio = MapsGosom()
    return etapas.Fontes(proprio, CnpjBaseLocal(conn), custo_zero.FonteBloqueada("Instagram (Apify)"),
                         custo_zero.FonteBloqueada("Anúncios (Apify)"), geocodificador(conn),
                         mapas={"gosom_maps": proprio, "apify_maps": custo_zero.FonteBloqueada("Maps (Apify)")})


def contato_ligado() -> bool:
    return os.environ.get("RADAR_CONTATO") == "1" and not custo_zero.ativo()


def fontes_reais(conn) -> etapas.Fontes:
    if custo_zero.ativo():
        return fontes_custo_zero(conn)
    from radar.fontes.anuncios_apify import AnunciosApify
    from radar.fontes.instagram_apify import InstagramApify
    from radar.fontes.maps_apify import MapsApify
    from radar.fontes.maps_gosom import MapsGosom
    # Maps: gosom no nosso servidor (grátis). Toda pesquisa nova usa o gosom (decisão do Philip, 17/11);
    # o Apify do Maps só fica para pesquisas antigas gravadas com fonte_maps = 'apify_maps' rodarem como eram.
    apify = MapsApify() if os.environ.get("APIFY_TOKEN") else None
    # Próprio NÃO cai sozinho na API (decisão do Philip, 20/10): se falhar, a pesquisa fica 'falhou'
    # com a mensagem e a tela oferece "Tentar de novo".
    proprio = MapsGosom()
    mapas = {"gosom_maps": proprio}
    if apify:
        mapas["apify_maps"] = apify
    return etapas.Fontes(proprio, fonte_cnpj(conn), InstagramApify(), AnunciosApify(), geocodificador(conn),
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
        if isinstance(e, (etapas.FalhaDoMapsProprio, custo_zero.FontePagaBloqueada)):
            vai_tentar = fila.falhar(conn, job, str(e), definitiva=True)
        else:
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
    """Jobs da fila + (se RADAR_CONTATO=1) despachante e conversador da Fase 3.

    O contato roda em UM processo só (RADAR_CONTATO=1 em uma réplica): o ritmo de envio depende disso.
    """
    from datetime import datetime, timezone

    from radar import conversa
    from radar.comercial import Comercial

    nome = f"{socket.gethostname()}:{os.getpid()}"
    conn = psycopg.connect(dsn, autocommit=True)
    f = fontes_reais(conn)
    contato = contato_ligado()
    comercial = Comercial() if contato else None
    log.info("worker %s no ar (contato %s%s)", nome, "ligado" if contato else "desligado",
             ", modo só dados abertos" if custo_zero.ativo() else "")
    while True:
        trabalhou = await processar_um(conn, f, nome)
        if contato:
            agora = datetime.now(timezone.utc)
            try:
                trabalhou |= bool(await conversa.despachar(conn, comercial, agora))
                trabalhou |= bool(await conversa.conversar(conn, comercial, agora))
            except Exception as e:                  # noqa: BLE001 — o laço não pode morrer
                log.warning("contato: %s", e)
        if not trabalhou:
            time.sleep(espera)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # cada requisição do httpx vira uma linha de log com a URL completa: fica só aviso/erro
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(laco(os.environ["RADAR_DATABASE_URL"]))
