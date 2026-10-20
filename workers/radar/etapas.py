"""Etapas do pipeline (um handler por tipo de job em radar.jobs).

Fluxo de um lead novo:
  minerar ─┬─> enriquecer_site ─> enriquecer_instagram ─(─> verificar_anuncios)─┐
           └─> enriquecer_cnpj ─────────────────────────────────────────────────┴─> finalizar
  Anúncios ativos ficaram para o futuro (decisão do Luis, 20/10/2026): a etapa só
  entra no fluxo com radar.config 'anuncios_ativos' = {"ligado": true} na agência.
  finalizar (faturamento + presença, status 'enriquecido') ─> pontuar ('pontuado')

Tudo é por AGÊNCIA (migration 003): cada linha gravada leva o agency_id do job.
Orçamento: Radar desligado na agência => nada roda; fonte paga só é chamada se o
gasto do mês + o previsto couber no teto (radar.agencias). Sem orçamento, o campo
fica 'nao_verificado' e o lead segue; o Maps continua pelo gosom (grátis).
Falha em uma etapa não trava o lead. Toda chamada externa grava custo em radar.custos_api.
"""

import json
import logging
import math
from dataclasses import asdict

import psycopg
from psycopg.rows import dict_row

from radar import cnpj_match, fila, geo
from radar.estimativas import DadosFaturamento, DadosPresenca, estimar_faturamento, presenca_digital
from radar.fontes.base import ConsultaMaps
from radar.pontuacao import FAIXAS_PADRAO, PARAMS_PADRAO, PESOS_PADRAO, LeadParaPontuar, Regra, pontuar
from radar.site import verificar as verificar_site
from radar.telefone import normalizar, whatsapp_de_link

log = logging.getLogger("radar.etapas")

ENRIQUECIMENTO = ("enriquecer_site", "enriquecer_cnpj", "enriquecer_instagram", "verificar_anuncios")
RAIO_PADRAO_CIDADE_KM = 15
LIMITE_CONFIANCA_IG = 60


class RadarDesligado(Exception):
    """A agência está com o Radar desligado: o job é cancelado, não falha."""


class FalhaDoMapsProprio(Exception):
    """O servidor próprio (gosom) não conseguiu buscar. Não há nova tentativa automática nem ida
    automática para a API: a pesquisa fica 'falhou' com esta mensagem e a tela oferece a API (Apify)."""

    MENSAGEM = ("O servidor próprio não conseguiu buscar no Google Maps ({detalhe}). "
                "Você pode tentar de novo mais tarde ou buscar pela API (Apify), que é paga.")


class Fontes:
    """Os adaptadores em uso. Trocar uma fonte = passar outro objeto aqui."""

    def __init__(self, maps, cnpj, instagram, anuncios, geocodificar, site=verificar_site, mapas=None):
        self.maps, self.cnpj, self.instagram, self.anuncios = maps, cnpj, instagram, anuncios
        self.geocodificar = geocodificar          # async (cidade, uf, agency_id) -> (lat, lng) | None
        self.site = site
        # plataforma escolhida na pesquisa (radar.pesquisas.fonte_maps): 'gosom_maps' | 'apify_maps'
        self.mapas = mapas or {}

    def maps_da_pesquisa(self, chave: str | None):
        return self.mapas.get(chave or "", self.maps)


def _cur(conn):
    return conn.cursor(row_factory=dict_row)


# ── orçamento por agência ────────────────────────────────────────────────
def orcamento(conn, agency_id) -> tuple[bool, float, float]:
    """(ativo, teto do mês em BRL, gasto do mês em BRL)."""
    row = conn.execute("select ativo, teto_mensal_brl from radar.agencias where agency_id = %s",
                       (agency_id,)).fetchone()
    if not row:
        return False, 0.0, 0.0
    gasto = conn.execute(
        """select coalesce(sum(gasto_brl), 0) from radar.gasto_mes
           where agency_id = %s and mes = date_trunc('month', now() at time zone 'America/Sao_Paulo')::date""",
        (agency_id,)).fetchone()[0]
    return bool(row[0]), float(row[1] or 0), float(gasto)


def exigir_ativo(conn, agency_id) -> None:
    if not orcamento(conn, agency_id)[0]:
        raise RadarDesligado(f"Radar desligado na agência {agency_id}")


def pode_gastar(conn, agency_id, previsto: float) -> bool:
    ativo, teto, gasto = orcamento(conn, agency_id)
    return ativo and gasto + previsto <= teto


def custo(conn, agency_id, fonte: str, operacao: str, valor: float, qtd: int = 1, *, pesquisa_id=None, lead_id=None):
    conn.execute(
        """insert into radar.custos_api (agency_id, fonte, operacao, pesquisa_id, lead_id, qtd, valor, estimado)
           values (%s, %s, %s, %s, %s, %s, %s, true)""",
        (agency_id, fonte, operacao, pesquisa_id, lead_id, qtd, valor))
    if pesquisa_id:
        conn.execute("update radar.pesquisas set custo_real = custo_real + %s where id = %s", (valor, pesquisa_id))


def _config(conn, agency_id, chave: str) -> dict:
    row = conn.execute("select valor from radar.config where agency_id = %s and chave = %s",
                       (agency_id, chave)).fetchone()
    return row[0] if row else {}


def anuncios_ligados(conn, agency_id) -> bool:
    return bool(_config(conn, agency_id, "anuncios_ativos").get("ligado"))


# ── F2 · minerar ─────────────────────────────────────────────────────────
async def minerar(conn: psycopg.Connection, job: fila.Job, f: Fontes) -> None:
    ag = job.agency_id
    p = _cur(conn).execute("select * from radar.pesquisas where id = %s", (job.pesquisa_id,)).fetchone()
    if p is None or p["status"] == "cancelada":
        return
    exigir_ativo(conn, ag)
    conn.execute("update radar.pesquisas set status = 'minerando' where id = %s", (p["id"],))
    filtros = p["filtros"] or {}
    maximo = int(filtros.get("max_resultados") or 100)
    nota_min = float(filtros.get("nota_minima") or 0)
    aval_min = int(filtros.get("min_avaliacoes") or 0)

    lat, lng, raio = p["lugar_lat"], p["lugar_lng"], float(p["raio_km"] or RAIO_PADRAO_CIDADE_KM)
    if lat is None:
        ponto = await f.geocodificar(p["lugar_cidade"], p["lugar_estado"], ag)
        if not ponto:
            raise RuntimeError(f"não achei a cidade {p['lugar_cidade']}-{p['lugar_estado']}")
        lat, lng = ponto
    maps = f.maps_da_pesquisa(p["fonte_maps"])
    limite = getattr(maps, "limite_por_consulta", geo.LIMITE_POR_CONSULTA)
    celulas = geo.grade(lat, lng, raio, maximo, limite_por_consulta=limite)
    por_celula = min(limite * 2, math.ceil(maximo * 1.3 / len(celulas)))
    termo = p["nicho_texto"] + (f" {p['categoria_maps']}" if p["categoria_maps"] else "")

    vistos, aceitos = set(), []
    for cel in celulas:
        previsto = maps.estimar(por_celula) if hasattr(maps, "estimar") else 0.0
        if hasattr(maps, "reserva"):
            # principal grátis sempre roda; a reserva paga só entra se couber no teto
            reserva = getattr(maps.reserva, "estimar", lambda n: 0.0)(por_celula)
            maps.permitir_reserva = pode_gastar(conn, ag, reserva)
        elif previsto and not pode_gastar(conn, ag, previsto):
            log.info("minerar %s: teto da agência atingido, parando a busca", p["id"])
            break
        try:
            negocios = await maps.buscar(ConsultaMaps(termo, cel.lat, cel.lng, cel.raio_km,
                                                      p["categoria_maps"], por_celula),
                                         uf_padrao=p["lugar_estado"])
        except Exception as e:
            if p["fonte_maps"] == "gosom_maps":
                raise FalhaDoMapsProprio(FalhaDoMapsProprio.MENSAGEM.format(detalhe=f"{type(e).__name__}: {e}"[:300])) from e
            raise
        custo(conn, ag, maps.chave, "buscar", maps.custo_ultima_chamada, len(negocios), pesquisa_id=p["id"])
        for n in negocios:
            if n.place_id in vistos:
                continue
            vistos.add(n.place_id)
            if n.fechado_permanentemente:
                continue
            if (n.nota or 0) < nota_min or (n.qtd_avaliacoes or 0) < aval_min:
                continue
            # raio vale pelo ponto central, não pela célula
            if n.lat is not None and geo.distancia_km(lat, lng, n.lat, n.lng) > raio * 1.05:
                continue
            aceitos.append(n)
        if len(aceitos) >= maximo:
            break

    novos = 0
    for n in aceitos[:maximo]:
        lead_id, novo = _gravar_lead(conn, ag, n, maps.chave)
        conn.execute("""insert into radar.pesquisas_leads (agency_id, pesquisa_id, lead_id) values (%s, %s, %s)
                        on conflict do nothing""", (ag, p["id"], lead_id))
        if novo:
            novos += 1
            for tipo in ("enriquecer_site", "enriquecer_cnpj"):
                fila.enfileirar(conn, tipo, agency_id=ag, pesquisa_id=p["id"], lead_id=lead_id)
    conn.execute("""update radar.pesquisas set status = 'enriquecendo', total_encontrado = %s where id = %s""",
                 (len(aceitos[:maximo]), p["id"]))
    log.info("minerar %s: %d aceitos, %d novos", p["id"], len(aceitos[:maximo]), novos)


def _gravar_lead(conn, ag, n, fonte: str) -> tuple[str, bool]:
    tel = normalizar(n.telefone)
    wa = whatsapp_de_link(n.whatsapp_link)
    row = conn.execute(
        """insert into radar.leads (agency_id, place_id, nome, categoria, nicho, endereco, cidade, estado, cep,
                 lat, lng, telefone, tem_whatsapp_provavel, whatsapp, site, nota, qtd_avaliacoes, horario,
                 link_maps, fonte_maps, site_status, dados_maps)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           on conflict (agency_id, place_id) do nothing returning id""",
        (ag, n.place_id, n.nome, n.categoria, n.categoria, n.endereco, n.cidade, n.estado or None, n.cep,
         n.lat, n.lng, tel.e164 if tel else None, bool(wa) or bool(tel and tel.celular), wa, n.site,
         n.nota, n.qtd_avaliacoes, json.dumps(n.horario) if n.horario else None, n.link_maps, fonte,
         "nao_verificado" if n.site else "nao_se_aplica", json.dumps(n.bruto) if n.bruto else None),
    ).fetchone()
    if row is None:
        return conn.execute("select id from radar.leads where agency_id = %s and place_id = %s",
                            (ag, n.place_id)).fetchone()[0], False
    lead_id = row[0]
    for a in n.avaliacoes[:5]:
        conn.execute("""insert into radar.avaliacoes (agency_id, lead_id, autor, nota, texto, data)
                        values (%s,%s,%s,%s,%s,%s)""", (ag, lead_id, a.autor or None, a.nota, a.texto, a.data))
    return lead_id, True


# ── F5 · site ────────────────────────────────────────────────────────────
async def enriquecer_site(conn, job: fila.Job, f: Fontes) -> None:
    ag = job.agency_id
    exigir_ativo(conn, ag)
    lead = _cur(conn).execute("select * from radar.leads where id = %s", (job.lead_id,)).fetchone()
    r = await f.site(lead["site"])
    conn.execute(
        """insert into radar.presenca (agency_id, lead_id, site_existe, site_responde, site_https, site_tem_whatsapp,
                 site_tem_meta_pixel, site_tem_google_tag, site_tem_google_ads, site_plataforma,
                 instagram_user, instagram_origem, facebook_pagina, redes, site_emails, fonte_site)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'site_proprio')""",
        (ag, lead["id"], r.site_existe, r.site_responde, r.site_https, r.site_tem_whatsapp, r.site_tem_meta_pixel,
         r.site_tem_google_tag, r.site_tem_google_ads, r.site_plataforma if r.site_responde else None,
         r.instagram_user, "site" if r.instagram_user else None, r.facebook_pagina,
         json.dumps(r.redes) if r.redes else None, r.emails or None))
    status = "nao_se_aplica" if not r.site_existe else ("encontrado" if r.site_responde else "nao_encontrado")
    email = r.emails[0] if r.emails else None
    conn.execute("""update radar.leads set site_status = %s,
                    whatsapp = coalesce(whatsapp, %s), tem_whatsapp_provavel = tem_whatsapp_provavel or %s,
                    email_origem = case when email is null and %s::text is not null then 'site' else email_origem end,
                    email = coalesce(email, %s)
                    where id = %s""", (status, r.whatsapp, bool(r.whatsapp), email, email, lead["id"]))
    fila.enfileirar(conn, "enriquecer_instagram", agency_id=ag, pesquisa_id=job.pesquisa_id, lead_id=lead["id"],
                    payload={"instagram": r.instagram_user, "facebook": r.facebook_pagina})


# ── F5 · Instagram ───────────────────────────────────────────────────────
def _similaridade_usuario(alvo: str, usuario: str) -> float:
    return cnpj_match._similar(alvo, cnpj_match.normalizar_nome(usuario.replace("_", " ").replace(".", " ")).replace(" ", ""))


async def enriquecer_instagram(conn, job: fila.Job, f: Fontes) -> None:
    ag = job.agency_id
    exigir_ativo(conn, ag)
    lead = _cur(conn).execute("select * from radar.leads where id = %s", (job.lead_id,)).fetchone()
    usuario, origem, confianca = job.payload.get("instagram"), "site", 100
    if job.payload.get("humano"):
        origem = "humano"
    sem_orcamento = False
    if not usuario:
        if pode_gastar(conn, ag, f.instagram.estimar(1)):
            candidatos = await f.instagram.procurar(lead["nome"], lead["cidade"] or "")
            custo(conn, ag, f.instagram.chave, "procurar", f.instagram.custo_ultima_chamada,
                  pesquisa_id=job.pesquisa_id, lead_id=lead["id"])
            alvo = cnpj_match.normalizar_nome(lead["nome"]).replace(" ", "")
            melhor = max(candidatos, default=None, key=lambda u: _similaridade_usuario(alvo, u))
            if melhor:
                usuario, origem = melhor, "busca_nome"
                confianca = int(_similaridade_usuario(alvo, melhor) * 100)
        else:
            sem_orcamento = True
    perfil = None
    if usuario and confianca >= LIMITE_CONFIANCA_IG:
        if pode_gastar(conn, ag, f.instagram.estimar(1)):
            perfil = await f.instagram.perfil(usuario)
            custo(conn, ag, f.instagram.chave, "perfil", f.instagram.custo_ultima_chamada,
                  pesquisa_id=job.pesquisa_id, lead_id=lead["id"])
        else:
            sem_orcamento = True
    site = conn.execute("""select site_existe, site_responde, site_https, site_tem_whatsapp, site_tem_meta_pixel,
                                  site_tem_google_tag, site_tem_google_ads, site_plataforma, facebook_pagina,
                                  redes, site_emails
                           from radar.presenca where lead_id = %s order by coletado_em desc, id desc limit 1""",
                        (lead["id"],)).fetchone() or (None,) * 11
    site = tuple(json.dumps(v) if isinstance(v, dict) else v for v in site)
    if perfil:
        conn.execute(
            """insert into radar.presenca (agency_id, lead_id, instagram_user, instagram_origem, instagram_confianca,
                     seguidores, seguindo, posts_total, ultimo_post_em, posts_30d, bio, bio_tem_link,
                     bio_tem_whatsapp, conta_comercial, fonte_instagram, dados_instagram,
                     site_existe, site_responde, site_https, site_tem_whatsapp, site_tem_meta_pixel,
                     site_tem_google_tag, site_tem_google_ads, site_plataforma, facebook_pagina, redes, site_emails)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ag, lead["id"], perfil.usuario, origem, confianca, perfil.seguidores, perfil.seguindo,
             perfil.posts_total, perfil.ultimo_post_em, perfil.posts_30d, perfil.bio, perfil.bio_tem_link,
             perfil.bio_tem_whatsapp, perfil.conta_comercial, f.instagram.chave,
             json.dumps(perfil.bruto) if perfil.bruto else None, *site))
    status = "encontrado" if perfil else ("nao_verificado" if sem_orcamento else "nao_encontrado")
    conn.execute("update radar.leads set instagram_status = %s where id = %s", (status, lead["id"]))
    if not job.payload.get("sem_anuncios") and anuncios_ligados(conn, ag):
        fila.enfileirar(conn, "verificar_anuncios", agency_id=ag, pesquisa_id=job.pesquisa_id, lead_id=lead["id"],
                        payload={"pagina": job.payload.get("facebook") or (perfil.usuario if perfil else None)})


# ── F6 · anúncios ────────────────────────────────────────────────────────
async def verificar_anuncios(conn, job: fila.Job, f: Fontes) -> None:
    ag = job.agency_id
    exigir_ativo(conn, ag)
    lead = _cur(conn).execute("select id, nome, status from radar.leads where id = %s", (job.lead_id,)).fetchone()
    if not pode_gastar(conn, ag, f.anuncios.estimar(30)):
        conn.execute("update radar.leads set anuncio_ativo = 'nao_verificado' where id = %s", (lead["id"],))
        return
    r = await f.anuncios.verificar(job.payload.get("pagina"), lead["nome"])
    custo(conn, ag, f.anuncios.chave, "verificar", f.anuncios.custo_ultima_chamada,
          pesquisa_id=job.pesquisa_id, lead_id=lead["id"])
    conn.execute(
        """insert into radar.anuncios (agency_id, lead_id, pagina_id, pagina_nome, qtd_ativos, mais_antigo_em,
                 link_biblioteca, amostras, fonte) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (ag, lead["id"], r.pagina_id, r.pagina_nome, r.qtd_ativos, r.mais_antigo_em, r.link_biblioteca,
         json.dumps(r.amostras), f.anuncios.chave))
    estado = "nao_verificado" if r.qtd_ativos is None else ("sim" if r.qtd_ativos > 0 else "nao")
    conn.execute("update radar.leads set anuncio_ativo = %s, qtd_anuncios_ativos = %s where id = %s",
                 (estado, r.qtd_ativos, lead["id"]))
    if lead["status"] != "encontrado":            # "Verificar de novo" num lead já pontuado
        fila.enfileirar(conn, "pontuar", agency_id=ag, lead_id=lead["id"])


# ── F3 · CNPJ ────────────────────────────────────────────────────────────
async def enriquecer_cnpj(conn, job: fila.Job, f: Fontes) -> None:
    ag = job.agency_id
    exigir_ativo(conn, ag)
    lead = _cur(conn).execute("select * from radar.leads where id = %s", (job.lead_id,)).fetchone()
    if not pode_gastar(conn, ag, f.cnpj.estimar(10)):
        conn.execute("update radar.leads set cnpj_status = 'nao_verificado' where id = %s", (lead["id"],))
        return
    cands = await f.cnpj.buscar_por_nome(lead["nome"], lead["cidade"] or "", lead["estado"] or "")
    custo(conn, ag, f.cnpj.chave, "buscar_por_nome", f.cnpj.custo_ultima_chamada, len(cands),
          pesquisa_id=job.pesquisa_id, lead_id=lead["id"])
    neg = cnpj_match.Negocio(lead["nome"], lead["endereco"], lead["cidade"] or "", lead["cep"] or "",
                             lead["telefone"] or "")
    por_cnpj = {c.cnpj: c for c in cands}
    dec = cnpj_match.decidir(neg, [cnpj_match.Candidato(
        c.cnpj, c.razao_social, c.nome_fantasia, c.logradouro, c.numero, c.municipio, c.cep,
        (normalizar(c.telefone_rf).e164 if normalizar(c.telefone_rf) else ""), c.situacao) for c in cands])
    escolhido = dec.escolhido.candidato.cnpj if dec.escolhido else None
    if escolhido and hasattr(f.cnpj, "detalhar"):
        det = await f.cnpj.detalhar(escolhido)
        if det:
            from radar.fontes.cnpj import completar_brasilapi
            completar_brasilapi(por_cnpj[escolhido], det)
    conn.execute("delete from radar.empresas where lead_id = %s", (lead["id"],))   # recoleta substitui
    for r in dec.candidatos:
        d = por_cnpj[r.candidato.cnpj]
        conn.execute(
            """insert into radar.empresas (agency_id, lead_id, escolhido, cnpj, razao_social, nome_fantasia, situacao,
                     abertura, porte, natureza, capital_social, cnae_principal, cnae_descricao,
                     cnaes_secundarios, regime, qtd_socios, socios, email_rf, telefone_rf, qtd_filiais,
                     eh_filial, cnpj_matriz, confianca_cnpj, motivo_confianca, fonte, dados_receita)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ag, lead["id"], d.cnpj == escolhido, d.cnpj, d.razao_social, d.nome_fantasia, d.situacao, d.abertura,
             d.porte, d.natureza, d.capital_social, d.cnae_principal, d.cnae_descricao,
             json.dumps(d.cnaes_secundarios), d.regime, len(d.socios), json.dumps(d.socios), d.email_rf,
             d.telefone_rf, d.qtd_filiais, d.eh_filial, d.cnpj_matriz, r.confianca, r.motivo, f.cnpj.chave,
             json.dumps(d.bruto) if d.bruto else None))
    if escolhido and por_cnpj[escolhido].email_rf:
        conn.execute("""update radar.leads set email = %s, email_origem = 'receita'
                        where id = %s and email is null""", (por_cnpj[escolhido].email_rf.lower(), lead["id"]))
    status = "encontrado" if escolhido else ("revisao" if dec.revisao_humana else "nao_encontrado")
    conn.execute("update radar.leads set cnpj_status = %s, revisao_cnpj = %s where id = %s",
                 (status, dec.revisao_humana, lead["id"]))


# ── finalizar enriquecimento e pontuar ───────────────────────────────────
def talvez_finalizar(conn, lead_id: str) -> bool:
    """Quando não há mais etapa de enriquecimento pendente, fecha o lead como 'enriquecido'."""
    with conn.transaction():
        st = conn.execute("select status, agency_id from radar.leads where id = %s for update", (lead_id,)).fetchone()
        if not st or st[0] != "encontrado":
            return False
        ag = st[1]
        abertos = conn.execute(
            """select count(*) filter (where status in ('pendente','rodando')),
                      count(*) filter (where tipo = 'enriquecer_cnpj'),
                      count(*) filter (where tipo = 'enriquecer_site'),
                      count(*) filter (where tipo = 'enriquecer_instagram'),
                      count(*) filter (where tipo = 'verificar_anuncios')
               from radar.jobs where lead_id = %s and tipo = any(%s)""", (lead_id, list(ENRIQUECIMENTO))).fetchone()
        pendentes, cnpj, site, instagram, anuncios = abertos
        if pendentes or not cnpj or not site:
            return False
        fim_da_cadeia = anuncios if anuncios_ligados(conn, ag) else instagram
        if not (fim_da_cadeia or _cadeia_morreu(conn, lead_id)):
            return False
        _calcular_estimativas(conn, ag, lead_id)
        conn.execute("update radar.leads set status = 'enriquecido' where id = %s", (lead_id,))
        fila.enfileirar(conn, "pontuar", agency_id=ag, lead_id=lead_id)
    return True


def _cadeia_morreu(conn, lead_id) -> bool:
    """Site ou Instagram falharam de vez: a cadeia não chega ao fim."""
    return conn.execute(
        """select exists(select 1 from radar.jobs where lead_id = %s and status in ('falhou', 'cancelado')
                          and tipo in ('enriquecer_site','enriquecer_instagram'))""", (lead_id,)).fetchone()[0]


def _calcular_estimativas(conn, ag, lead_id) -> None:
    lead = _cur(conn).execute("select * from radar.leads where id = %s", (lead_id,)).fetchone()
    emp = _cur(conn).execute("select * from radar.empresas where lead_id = %s and escolhido", (lead_id,)).fetchone()
    pres = _cur(conn).execute("""select * from radar.presenca where lead_id = %s
                                 order by coletado_em desc, id desc limit 1""", (lead_id,)).fetchone()
    unidades = 1
    if emp and emp["cnpj_matriz"]:
        unidades = conn.execute("""select count(distinct lead_id) from radar.empresas
                                   where agency_id = %s and escolhido and cnpj_matriz = %s""",
                                (ag, emp["cnpj_matriz"])).fetchone()[0] or 1
    est = estimar_faturamento(DadosFaturamento(
        porte=emp["porte"] if emp else None, capital_social=float(emp["capital_social"]) if emp and emp["capital_social"] else None,
        qtd_avaliacoes=lead["qtd_avaliacoes"], abertura=emp["abertura"] if emp else None, unidades=unidades,
        cnae=emp["cnae_principal"] if emp else None, tem_site=lead["site_status"] == "encontrado",
        seguidores=pres["seguidores"] if pres else None), _config(conn, ag, "pesos_faturamento"))
    pd = presenca_digital(DadosPresenca(
        instagram_verificado=lead["instagram_status"] != "nao_verificado",
        tem_instagram=bool(pres and pres["instagram_user"] and pres["seguidores"] is not None),
        seguidores=pres["seguidores"] if pres else None, posts_30d=pres["posts_30d"] if pres else None,
        site_verificado=lead["site_status"] != "nao_verificado",
        site_responde=bool(pres and pres["site_responde"]),
        tem_pixel_ou_tag=bool(pres and (pres["site_tem_meta_pixel"] or pres["site_tem_google_tag"]))),
        _config(conn, ag, "regra_presenca"))
    conn.execute("""update radar.leads set faturamento_estimado_faixa = %s, faturamento_origem = %s,
                    faturamento_explicacao = %s, presenca_digital = %s where id = %s""",
                 (est.faixa, est.origem, est.explicacao, pd, lead_id))


def regra_vigente(conn, agency_id, nicho: str | None) -> tuple[Regra, int]:
    """Prioridade: regra da agência para o nicho > geral da agência > padrão RCO do nicho > padrão RCO geral.

    Dentro de cada nível vale a versão mais recente. Sem nenhuma regra, cria a padrão RCO v1.
    """
    rows = _cur(conn).execute(
        """select id, versao, nicho, pesos, faixas, agency_id from radar.regras_pontuacao
           where agency_id = %s or agency_id is null""", (agency_id,)).fetchall()
    if not rows:
        rid = conn.execute("""insert into radar.regras_pontuacao (agency_id, versao, nicho, pesos, faixas)
                              values (null, 1, null, %s, %s) returning id""",
                           (json.dumps(PESOS_PADRAO), json.dumps({**FAIXAS_PADRAO, **PARAMS_PADRAO}))).fetchone()[0]
        return Regra(1), rid
    alvo = (nicho or "").strip().lower()

    def nivel(r):
        do_nicho = bool(r["nicho"]) and r["nicho"].strip().lower() == alvo
        if r["nicho"] and not do_nicho:
            return None                          # regra de outro nicho não se aplica
        da_agencia = r["agency_id"] is not None
        return (2 if da_agencia else 0) + (1 if do_nicho else 0)

    candidatas = [(nivel(r), r) for r in rows if nivel(r) is not None]
    if not candidatas:
        return regra_vigente_padrao(conn)
    _, r = max(candidatas, key=lambda t: (t[0], t[1]["versao"]))
    faixas = r["faixas"] or {}
    regra = Regra(r["versao"], r["pesos"] or dict(PESOS_PADRAO),
                  {k: faixas.get(k, v) for k, v in FAIXAS_PADRAO.items()},
                  {k: faixas.get(k, v) for k, v in PARAMS_PADRAO.items()}, r["nicho"])
    return regra, r["id"]


def regra_vigente_padrao(conn) -> tuple[Regra, int]:
    rid = conn.execute("""insert into radar.regras_pontuacao (agency_id, versao, nicho, pesos, faixas)
                          values (null, 1, null, %s, %s)
                          on conflict do nothing returning id""",
                       (json.dumps(PESOS_PADRAO), json.dumps({**FAIXAS_PADRAO, **PARAMS_PADRAO}))).fetchone()
    if rid is None:
        rid = conn.execute("""select id from radar.regras_pontuacao where agency_id is null and nicho is null
                              and versao = 1""").fetchone()
    return Regra(1), rid[0]


async def pontuar_lead(conn, job: fila.Job, f: Fontes | None = None) -> None:
    lead = _cur(conn).execute("select * from radar.leads where id = %s", (job.lead_id,)).fetchone()
    emp = conn.execute("select situacao from radar.empresas where lead_id = %s and escolhido", (lead["id"],)).fetchone()
    ig = conn.execute("""select instagram_user from radar.presenca where lead_id = %s and instagram_user is not null
                         order by coletado_em desc, id desc limit 1""", (lead["id"],)).fetchone()
    regra, regra_id = regra_vigente(conn, lead["agency_id"], lead["nicho"])
    pt = pontuar(LeadParaPontuar(
        anuncio_ativo=lead["anuncio_ativo"], presenca_digital=lead["presenca_digital"],
        tem_whatsapp_provavel=lead["tem_whatsapp_provavel"], whatsapp=lead["whatsapp"],
        instagram_user=ig[0] if ig else None, telefone=lead["telefone"],
        faturamento_estimado_faixa=lead["faturamento_estimado_faixa"], nota=float(lead["nota"] or 0),
        qtd_avaliacoes=lead["qtd_avaliacoes"], cnpj_situacao=emp[0] if emp else None), regra)
    conn.execute("""insert into radar.pontuacoes (agency_id, lead_id, versao_regra, regra_id, criterios, total)
                    values (%s,%s,%s,%s,%s,%s)""",
                 (lead["agency_id"], lead["id"], pt.versao_regra, regra_id,
                  json.dumps([asdict(c) for c in pt.criterios]), pt.total))
    conn.execute("""update radar.leads set pontuacao = %s, classificacao = %s, versao_regra = %s,
                    status = case when status = 'enriquecido' then 'pontuado' else status end
                    where id = %s""", (pt.total, pt.classificacao, pt.versao_regra, lead["id"]))


HANDLERS = {
    "minerar": minerar,
    "enriquecer_site": enriquecer_site,
    "enriquecer_instagram": enriquecer_instagram,
    "verificar_anuncios": verificar_anuncios,
    "enriquecer_cnpj": enriquecer_cnpj,
    "pontuar": pontuar_lead,
}

# o que marcar quando a etapa falha de vez (falha não trava o lead)
AO_FALHAR = {
    "enriquecer_site": "update radar.leads set site_status = 'nao_verificado' where id = %s",
    "enriquecer_instagram": "update radar.leads set instagram_status = 'nao_verificado' where id = %s",
    "verificar_anuncios": "update radar.leads set anuncio_ativo = 'nao_verificado' where id = %s",
    "enriquecer_cnpj": "update radar.leads set cnpj_status = 'nao_verificado' where id = %s",
}
