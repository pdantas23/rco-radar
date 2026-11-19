"""Fase 3 · contato por IA: despachante (1ª mensagem no ritmo certo) e conversador (IA responde).

Desenho aprovado pelo Philip em 20/10/2026:
- envio sempre pela rota interna do COMERCIAL (a conversa aparece no inbox, mensagens como "bot");
- as mensagens vivem só no inbox (public.messages); o Radar guarda estado, campos e ritmo em radar.conversas;
- humano responde no inbox ou no celular = IA para naquela conversa;
- negócio no funil só quando o lead é qualificado; distribuição para atendente só quando a IA passa.

Nada sai sem: disparo confirmado por humano, registro em disparos_leads antes da tentativa,
janela de horário da agência, limites por hora/dia da conexão e a trava de radar.gate.

Triagem IA (docs/triagem-ia.md): conversa de LP (origem 'lp', sem radar.leads) aberta pelo webhook do
COMERCIAL recebe a 1ª mensagem automaticamente (a integração marcada para triagem é a autorização), com
a mesma janela, limites e trava; o registro prévio fica em conversas.contexto.primeira.

Triagem v2 (seção v2 do contrato; vale para as origens em radar.config 'triagem_regras'.origens, padrão LP e Radar):
a cada resposta a decisão sai dos portões de radar.triagem (fatos da IA + regras da agência), não da IA:
passa_direto (oferece 2 horários do Google do SDR → agenda → passa), passa_alerta/redireciona (passa com alerta,
sem horário), esteira (30/60/90 dias) ou desqualifica. Sem resposta: cadência de 5 toques só seg-sex 9h-18h no
fuso da agência. No Radar a 1ª mensagem continua sendo a abordagem do disparo (toque 1) e a ficha do negócio
(Maps/CNPJ/site) entra como fatos já conhecidos. Agência que tira 'radar' das origens volta ao fluxo antigo (a
intenção da IA decide). Agenda: só oferece horário se o COMERCIAL devolver 2 opções (/horarios); sem Google
configurado ou sem SDR conectado, passa ao SDR com "passar_sem_horario", sem prometer dia nem hora.
"""

import json
import logging
import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from radar import gate, ia, toque_humano, triagem
from radar.comercial import Comercial

log = logging.getLogger("radar.conversa")

# Padrão dos acompanhamentos: 24h depois da 1ª e +48h (= 72h); depois do último, mais 48h e encerra.
# A agência troca em radar.config 'triagem_followups' = {"quantidade": n, "intervalos_horas": [...]}.
FOLLOWUPS = (timedelta(hours=24), timedelta(hours=48))
MAX_FOLLOWUPS = 2
MAX_FOLLOWUPS_CONFIG = 5


def followups(conn, agency_id) -> tuple[int, tuple[timedelta, ...]]:
    """(quantidade, intervalos) da agência; padrão quando a chave falta ou é inválida (quantidade 0-5,
    intervalos positivos, pelo menos um por acompanhamento)."""
    row = conn.execute("select valor from radar.config where agency_id = %s and chave = 'triagem_followups'",
                       (agency_id,)).fetchone()
    v = row[0] if row else None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = None
    if isinstance(v, dict):
        q, horas = v.get("quantidade"), v.get("intervalos_horas")
        num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)   # noqa: E731
        if (num(q) and int(q) == q and 0 <= q <= MAX_FOLLOWUPS_CONFIG and isinstance(horas, list)
                and all(num(h) and h > 0 for h in horas) and len(horas) >= q):
            return int(q), tuple(timedelta(hours=h) for h in horas) or FOLLOWUPS[:1]
    return MAX_FOLLOWUPS, FOLLOWUPS


def _espera(intervalos, i: int) -> timedelta:
    """Espera antes do acompanhamento i+1 (i = quantos já saíram); depois do último, repete o último intervalo."""
    return intervalos[min(i, len(intervalos) - 1)]
JANELA_PADRAO = (time(8), time(20))


def _cur(conn):
    return conn.cursor(row_factory=dict_row)


def _agencia(conn, agency_id) -> dict:
    return _cur(conn).execute(
        """select a.*, ag.name as nome_agencia from radar.agencias a
           join public.agencies ag on ag.id = a.agency_id where a.agency_id = %s""", (agency_id,)).fetchone() or {}


def _agora_local(cfg: dict, agora: datetime) -> datetime:
    return agora.astimezone(ZoneInfo(cfg.get("fuso") or "America/Sao_Paulo")).replace(tzinfo=None)


def _intervalo(disparo: dict) -> float:
    minimo = disparo["intervalo_seg"]
    maximo = disparo["intervalo_max_seg"] or minimo
    if disparo["aleatorio"] and maximo > minimo:
        return random.uniform(minimo, maximo)
    return float(minimo)


def preencher(texto: str, lead: dict) -> str:
    ponto = lead.get("ponto_da_ficha") or (
        f"vocês têm nota {str(lead['nota']).replace('.', ',')} no Google" if lead.get("nota") else "")
    valores = {"nome_negocio": lead.get("nome") or "", "cidade": lead.get("cidade") or "",
               "nicho": lead.get("nicho") or "", "ponto_da_ficha": ponto}
    for k, v in valores.items():
        texto = texto.replace("{" + k + "}", str(v))
    return texto


def _empresa(conn, lead_id) -> dict | None:
    """CNPJ escolhido do lead (confiança >= 50), se houver."""
    return _cur(conn).execute(
        """select razao_social, nome_fantasia, abertura, porte, cnae_descricao, qtd_socios from radar.empresas
           where lead_id = %s and escolhido limit 1""", (lead_id,)).fetchone()


def _ficha_v2(conn, lead: dict) -> str:
    """Triagem v2 no Radar: tudo o que a ficha já sabe do negócio, para a IA não perguntar de novo. Sem o
    faturamento estimado (estimativa do sistema: a faixa é perguntada ao lead, por último)."""
    linhas = [f"Negócio: {lead['nome']}",
              f"Segmento (Google Maps): {lead.get('nicho') or lead.get('categoria') or 'não informado'}",
              f"Cidade: {lead.get('cidade') or 'não informada'}" + (f"-{lead['estado']}" if lead.get("estado") else ""),
              f"Endereço: {lead.get('endereco') or 'não informado'}"]
    if lead.get("nota"):
        linhas.append(f"Google: nota {lead['nota']} com {lead.get('qtd_avaliacoes') or 0} avaliações")
    if lead.get("site"):
        linhas.append(f"Site: {lead['site']}")
    pres = _cur(conn).execute("""select instagram_user, seguidores from radar.presenca where lead_id = %s
                                 order by coletado_em desc, id desc limit 1""", (lead["id"],)).fetchone()
    if pres and pres["instagram_user"]:
        linhas.append(f"Instagram: @{pres['instagram_user']}"
                      + (f" ({pres['seguidores']} seguidores)" if pres["seguidores"] is not None else ""))
    if lead.get("presenca_digital") and lead["presenca_digital"] != "nao_verificado":
        linhas.append(f"Presença digital: {lead['presenca_digital']}")
    if lead.get("anuncio_ativo") in ("sim", "nao"):
        linhas.append(f"Anúncio ativo na Meta: {lead['anuncio_ativo']}")
    emp = _empresa(conn, lead["id"])
    if emp:
        partes = [emp["razao_social"] or emp["nome_fantasia"] or "",
                  f"aberto em {emp['abertura'].year}" if emp["abertura"] else "",
                  f"porte {emp['porte']}" if emp["porte"] else "", emp["cnae_descricao"] or "",
                  f"{emp['qtd_socios']} sócio(s)" if emp["qtd_socios"] else ""]
        linhas.append("CNPJ: " + ", ".join(p for p in partes if p))
    aval = [r[0] for r in conn.execute("""select texto from radar.avaliacoes where lead_id = %s
                                          order by data desc nulls last limit 3""", (lead["id"],))]
    if aval:
        linhas.append("Avaliações recentes: " + " | ".join(a[:200] for a in aval))
    linhas.append("Esses dados são públicos e já conhecidos: NÃO pergunte de novo (ex.: a cidade e o segmento); "
                  "use para aprofundar. A conversa começou pela mensagem de abordagem do time.")
    return "\n".join(linhas)


def _ficha(conn, lead: dict) -> str:
    aval = [r[0] for r in conn.execute("""select texto from radar.avaliacoes where lead_id = %s
                                          order by data desc nulls last limit 3""", (lead["id"],))]
    linhas = [f"Negócio: {lead['nome']} ({lead.get('categoria') or lead.get('nicho') or ''}), {lead.get('cidade') or ''}-{lead.get('estado') or ''}",
              f"Google: nota {lead.get('nota')} com {lead.get('qtd_avaliacoes')} avaliações",
              f"Presença digital: {lead.get('presenca_digital')}; faturamento estimado: {lead.get('faturamento_estimado_faixa')}"]
    if aval:
        linhas.append("Avaliações recentes: " + " | ".join(a[:200] for a in aval))
    return "\n".join(linhas)


# ── despachante: 1ª mensagem ─────────────────────────────────────────────
async def despachar(conn, comercial: Comercial, agora: datetime) -> int:
    """1ªs mensagens: lotes do Radar (uma por disparo ativo por rodada) e conversas de LP. Devolve quantas enviou."""
    return await _despachar_radar(conn, comercial, agora) + await despachar_lp(conn, comercial, agora)


async def _despachar_radar(conn, comercial: Comercial, agora: datetime) -> int:
    enviados = 0
    disparos = _cur(conn).execute(
        """select d.*, ab.texto as abordagem_texto from radar.disparos d
           join radar.abordagens ab on ab.id = d.abordagem_id
           where d.status = 'fila' and d.confirmado_em is not null order by d.id""").fetchall()
    for d in disparos:
        cfg = _agencia(conn, d["agency_id"])
        if not cfg or not cfg.get("ativo"):
            continue
        if not (d["abordagem_texto"] or "").strip():
            # mensagem inicial ainda não definida: o lote espera, nada falha nem sai
            log.info("disparo %s aguardando a mensagem inicial da agência", d["id"])
            continue
        local = _agora_local(cfg, agora)
        inicio, fim = cfg.get("janela_inicio") or JANELA_PADRAO[0], cfg.get("janela_fim") or JANELA_PADRAO[1]
        if not (inicio <= local.time() < fim):
            continue
        if _regras_v2(conn, {"agency_id": d["agency_id"], "origem": "radar"}) is not None and not triagem.na_janela(
                agora, _fuso(cfg), triagem.cadencia(conn, d["agency_id"])["janela"]):
            continue                                # v2: toque 1 (a abordagem) também só seg-sex 9h-18h
        conexao = d["whatsapp_config_id"] or cfg.get("whatsapp_config_id")
        if conexao is None:
            log.warning("disparo %s sem conexão de WhatsApp (nem no disparo nem na Configuração do Radar)", d["id"])
            continue
        if not _dentro_dos_limites(conn, conexao, d, cfg, agora):
            continue
        item = _cur(conn).execute(
            """select dl.id, dl.lead_id from radar.disparos_leads dl
               where dl.disparo_id = %s and dl.status = 'pendente'
                 and coalesce(dl.agendado_para, '-infinity') <= %s
               order by dl.agendado_para nulls first, dl.id limit 1 for update skip locked""",
            (d["id"], agora)).fetchone()
        if item is None:
            if not conn.execute("""select exists(select 1 from radar.disparos_leads
                                   where disparo_id = %s and status in ('pendente','enviando'))""",
                                (d["id"],)).fetchone()[0]:
                conn.execute("update radar.disparos set status = 'concluido' where id = %s", (d["id"],))
            continue
        if not await _enviar_primeira(conn, comercial, d, cfg, conexao, item, agora, local):
            continue                                # nada saiu: o próximo pode ir na próxima rodada
        enviados += 1
        # nenhum outro deste disparo sai antes do intervalo (fixo ou aleatório)
        conn.execute("""update radar.disparos_leads
                        set agendado_para = greatest(coalesce(agendado_para, '-infinity'), %s)
                        where disparo_id = %s and status = 'pendente'""",
                     (agora + timedelta(seconds=_intervalo(d)), d["id"]))
    return enviados


def _dentro_dos_limites(conn, conexao, disparo, cfg, agora) -> bool:
    """1ªs mensagens na conexão (lotes do Radar + LP) dentro dos limites por hora/dia."""
    hora, dia = conn.execute(
        """select count(*) filter (where t > %s - interval '1 hour'), count(*) filter (where t > %s - interval '1 day')
           from (select dl.enviado_em as t
                   from radar.disparos_leads dl join radar.disparos d on d.id = dl.disparo_id
                   join radar.agencias a on a.agency_id = d.agency_id
                  where dl.status = 'enviado' and coalesce(d.whatsapp_config_id, a.whatsapp_config_id) = %s
                 union all
                 select c.iniciada_em from radar.conversas c join radar.agencias a on a.agency_id = c.agency_id
                  where c.origem = 'lp' and c.iniciada_em is not null
                    and coalesce(c.whatsapp_config_id, a.whatsapp_config_id) = %s) x""",
        (agora, agora, conexao, conexao)).fetchone()
    # O limite por hora do ENVIO manda (decisão do Philip, 19/11/2026: sem teto da agência por cima). O da agência
    # só vale quando não há envio (1ª mensagem de LP).
    lim_hora = (disparo or {}).get("limite_hora") or cfg.get("limite_hora") or 10**6
    return hora < lim_hora and dia < (cfg.get("limite_dia") or 10**6)


async def _enviar_primeira(conn, comercial, d, cfg, conexao, item, agora, local) -> bool:
    lead = _cur(conn).execute("select * from radar.leads where id = %s", (item["lead_id"],)).fetchone()
    telefone = lead["whatsapp"] or lead["telefone"]
    bloqueado = conn.execute(
        "select exists(select 1 from radar.bloqueios where agency_id = %s and telefone = %s)",
        (d["agency_id"], telefone)).fetchone()[0]
    ultimo = conn.execute(
        """select max(dl.enviado_em) from radar.disparos_leads dl
           where dl.lead_id = %s and dl.status = 'enviado'""", (lead["id"],)).fetchone()[0]
    texto = preencher(d["abordagem_texto"], lead)
    trava = gate.checar(texto, gate.Contexto(
        lead_status=lead["status"], motivo_descarte=lead["motivo_descarte"], bloqueado=bloqueado,
        ultimo_contato_mesmo_canal=ultimo.astimezone(ZoneInfo(cfg.get("fuso") or "America/Sao_Paulo")).replace(tzinfo=None) if ultimo else None,
        confirmado_recontato=bool((d["filtro"] or {}).get("confirmado_recontato")),
        disparo_confirmado=d["confirmado_em"] is not None, registrado_em_disparos_leads=True,
        agora_local_lead=local, janela_inicio=cfg.get("janela_inicio") or JANELA_PADRAO[0],
        janela_fim=cfg.get("janela_fim") or JANELA_PADRAO[1], canal="whatsapp_nao_oficial",
        template_aprovado=False, primeira_mensagem=True))
    if not telefone:
        trava = gate.Resultado(False, ["lead sem telefone/WhatsApp"])
    if not trava:
        conn.execute("update radar.disparos_leads set status = 'falhou', motivo_falha = %s where id = %s",
                     ("; ".join(trava.motivos)[:500], item["id"]))
        return False
    # registro ANTES da tentativa (regra de arquitetura)
    conn.execute("update radar.disparos_leads set status = 'enviando' where id = %s", (item["id"],))
    try:
        r = await comercial.enviar(agency_id=d["agency_id"], lead_id=lead["id"], whatsapp_config_id=conexao,
                                   telefone=telefone, nome=lead["nome"], texto=texto, primeira=True,
                                   idempotency_key=f"radar-dl-{item['id']}")
    except Exception as e:                         # noqa: BLE001 — erro de rede: volta para a fila
        log.warning("envio do item %s falhou (%s); volta para pendente", item["id"], e)
        conn.execute("""update radar.disparos_leads set status = 'pendente', motivo_falha = %s,
                        agendado_para = %s where id = %s""", (str(e)[:500], agora + timedelta(minutes=5), item["id"]))
        return False
    if not r.ok:
        conn.execute("update radar.disparos_leads set status = 'falhou', motivo_falha = %s where id = %s",
                     (r.motivo or "erro_envio", item["id"]))
        if r.motivo == "numero_invalido":
            conn.execute("""update radar.leads set status = 'descartado', motivo_descarte = 'numero_invalido'
                            where id = %s""", (lead["id"],))
        return False
    conn.execute("update radar.disparos_leads set status = 'enviado', enviado_em = %s where id = %s",
                 (agora, item["id"]))
    conn.execute("update radar.leads set status = 'contatado' where id = %s and status in ('pontuado','aprovado_para_contato')",
                 (lead["id"],))
    conn.execute(
        """insert into radar.conversas (agency_id, lead_id, estado, conversation_id, contact_id, whatsapp_config_id,
                 iniciada_em, lida_ate, proximo_followup_em, followups_enviados, cadencia_inicio)
           values (%s,%s,'ia',%s,%s,%s,%s,%s,%s,0,%s)
           on conflict (lead_id) do update set estado = 'ia', conversation_id = excluded.conversation_id,
                 contact_id = excluded.contact_id, whatsapp_config_id = excluded.whatsapp_config_id,
                 iniciada_em = excluded.iniciada_em, lida_ate = excluded.lida_ate,
                 proximo_followup_em = excluded.proximo_followup_em, followups_enviados = 0, motivo_fim = null,
                 cadencia_inicio = excluded.cadencia_inicio""",
        (d["agency_id"], lead["id"], r.conversation_id, r.contact_id, conexao, agora, agora,
         _proximo_apos_primeira(conn, {"agency_id": d["agency_id"], "origem": "radar"}, cfg, agora), agora))
    return True


# ── despachante de LP: 1ª mensagem automática ────────────────────────────
def _primeira(conn, conversa_id, **estado) -> None:
    """Registro da 1ª mensagem de LP em contexto.primeira (status pendente | enviando | enviado | falhou)."""
    conn.execute("""update radar.conversas set contexto = jsonb_set(contexto, '{primeira}',
                        coalesce(contexto->'primeira', '{}') || %s::jsonb) where id = %s""",
                 (json.dumps(estado, default=str), conversa_id))


async def despachar_lp(conn, comercial: Comercial, agora: datetime, max_conversas: int = 20) -> int:
    """Conversas de LP abertas pelo webhook e ainda sem 1ª mensagem. Sem aprovação humana (contrato), mas com
    janela, limites da agência/conexão, intervalo mínimo e a trava de radar.gate."""
    # 1ª mensagem que falhou e ainda não chegou ao funil: tenta de novo o /passar (o lead não se perde)
    for c in _cur(conn).execute(
            """select c.* from radar.conversas c
               where c.origem = 'lp' and c.estado = 'ia' and c.iniciada_em is null
                 and c.contexto->'primeira'->>'status' = 'falhou'
                 and coalesce((c.contexto->'primeira'->>'repassar_em')::timestamptz, '-infinity') <= %s
               order by c.id limit %s for update skip locked""", (agora, max_conversas)).fetchall():
        await _passar_falha(conn, comercial, c, c["contexto"]["primeira"].get("motivo") or "erro_envio", agora)
    enviados = 0
    pendentes = _cur(conn).execute(
        """select c.* from radar.conversas c
           where c.origem = 'lp' and c.estado = 'ia' and c.iniciada_em is null
             and coalesce(c.contexto->'primeira'->>'status', 'pendente') = 'pendente'
             and coalesce((c.contexto->'primeira'->>'agendado_para')::timestamptz, '-infinity') <= %s
           order by c.id limit %s for update skip locked""", (agora, max_conversas)).fetchall()
    for c in pendentes:
        cfg = _agencia(conn, c["agency_id"])
        if not cfg or not cfg.get("ativo"):
            continue
        local = _agora_local(cfg, agora)
        inicio, fim = cfg.get("janela_inicio") or JANELA_PADRAO[0], cfg.get("janela_fim") or JANELA_PADRAO[1]
        if not (inicio <= local.time() < fim):
            continue                                # espera a janela abrir; não é falha
        if _regras_v2(conn, c) is not None and not triagem.na_janela(
                agora, _fuso(cfg), triagem.cadencia(conn, c["agency_id"])["janela"]):
            continue                                # v2: toque 1 só seg-sex 9h-18h
        conexao = c["whatsapp_config_id"] or cfg.get("whatsapp_config_id")
        if conexao is None:
            log.warning("conversa LP %s sem conexão de WhatsApp", c["id"])
            continue
        if not _dentro_dos_limites(conn, conexao, None, cfg, agora):
            continue
        ultima = conn.execute("""select max(iniciada_em) from radar.conversas
                                 where origem = 'lp' and whatsapp_config_id = %s""", (conexao,)).fetchone()[0]
        if ultima and agora - ultima < timedelta(seconds=cfg.get("intervalo_min_seg") or 0):
            continue
        if await _enviar_primeira_lp(conn, comercial, c, cfg, conexao, agora, local):
            enviados += 1
    return enviados


async def _enviar_primeira_lp(conn, comercial, c, cfg, conexao, agora, local) -> bool:
    ctx = c["contexto"] or {}
    telefone = ctx.get("telefone")                 # opcional: o COMERCIAL usa o telefone do contato
    bloqueado = bool(telefone) and conn.execute(
        "select exists(select 1 from radar.bloqueios where agency_id = %s and telefone = %s)",
        (c["agency_id"], telefone)).fetchone()[0]
    texto = triagem.mensagem(conn, c["agency_id"], "lp", "inicial", cfg.get("nome_agencia") or "agência") or ""
    trava = gate.checar(texto, gate.Contexto(
        lead_status="lp", motivo_descarte=None, bloqueado=bloqueado, ultimo_contato_mesmo_canal=None,
        confirmado_recontato=False,
        disparo_confirmado=True,                   # a integração marcada para triagem é a autorização (contrato)
        registrado_em_disparos_leads=True,         # o registro é a própria conversa (contexto.primeira)
        agora_local_lead=local, janela_inicio=cfg.get("janela_inicio") or JANELA_PADRAO[0],
        janela_fim=cfg.get("janela_fim") or JANELA_PADRAO[1], canal="whatsapp_nao_oficial",
        template_aprovado=False, primeira_mensagem=True))
    if not trava:
        await _falhou_primeira(conn, comercial, c, "; ".join(trava.motivos), agora)
        return False
    # registro ANTES da tentativa (regra de arquitetura): se o processo cair aqui, não reenvia sozinho
    _primeira(conn, c["id"], status="enviando", em=agora.isoformat())
    try:
        r = await comercial.enviar(agency_id=c["agency_id"], lead_id=None, conversa_id=str(c["id"]),
                                   whatsapp_config_id=conexao, telefone=telefone, nome=ctx.get("nome") or "",
                                   texto=texto, primeira=True, idempotency_key=f"radar-lp-{c['id']}")
    except Exception as e:                         # noqa: BLE001 — erro de rede: volta para a fila
        log.warning("1ª mensagem da conversa LP %s falhou (%s); tenta de novo em 5 min", c["id"], e)
        _primeira(conn, c["id"], status="pendente", motivo=str(e)[:500],
                  agendado_para=(agora + timedelta(minutes=5)).isoformat())
        return False
    if not r.ok:
        await _falhou_primeira(conn, comercial, c, r.motivo or "erro_envio", agora)
        return False
    _primeira(conn, c["id"], status="enviado", em=agora.isoformat())
    conn.execute("""update radar.conversas set iniciada_em = %s, lida_ate = %s, proximo_followup_em = %s,
                        followups_enviados = 0, whatsapp_config_id = %s, cadencia_inicio = %s,
                        conversation_id = coalesce(conversation_id, %s), contact_id = coalesce(contact_id, %s)
                    where id = %s""",
                 (agora, agora, _proximo_apos_primeira(conn, c, cfg, agora), conexao, agora, r.conversation_id,
                  r.contact_id, c["id"]))
    return True


def _proximo_apos_primeira(conn, c, cfg, agora) -> datetime:
    """Triagem v2: toque 2 da cadência (+2 h, dentro da janela). Fora da v2: 1º acompanhamento da config antiga."""
    if _regras_v2(conn, c) is not None:
        cad = triagem.cadencia(conn, c["agency_id"])
        if len(cad["toques"]) > 1:
            return triagem.vencimento_toque(cad, 1, agora, agora, _fuso(cfg))
        return triagem.proxima_janela(agora + timedelta(hours=cad["encerrar_apos_horas"]), _fuso(cfg), cad["janela"])
    return agora + _espera(followups(conn, c["agency_id"])[1], 0)


async def _falhou_primeira(conn, comercial, c, motivo, agora) -> None:
    log.warning("1ª mensagem da conversa LP %s não saiu: %s", c["id"], motivo)
    _primeira(conn, c["id"], status="falhou", motivo=motivo[:500], em=agora.isoformat())
    await _passar_falha(conn, comercial, c, motivo[:500], agora)


async def _passar_falha(conn, comercial, c, motivo, agora) -> None:
    """O lead vai ao funil como hoje (/passar sem conversa do inbox). Se o COMERCIAL não aceitar ou cair, a
    conversa fica como está (estado 'ia', primeira 'falhou') e tenta de novo em 5 min."""
    try:
        r = await comercial.passar(agency_id=c["agency_id"], lead_id=None, conversation_id=None,
                                   conversa_id=str(c["id"]), motivo="falha_primeira_mensagem", campos={}, resumo=motivo)
        ok = bool((r or {}).get("ok"))
    except Exception as e:                         # noqa: BLE001
        log.warning("passar da conversa LP %s falhou (%s); tenta de novo em 5 min", c["id"], e)
        ok = False
    if not ok:
        _primeira(conn, c["id"], repassar_em=(agora + timedelta(minutes=5)).isoformat())
        return
    conn.execute("""update radar.conversas set estado = 'humano', motivo_fim = 'falha_primeira_mensagem'
                    where id = %s""", (c["id"],))


# ── conversador: IA responde e acompanha ─────────────────────────────────
async def conversar(conn, comercial: Comercial, agora: datetime, cliente_ia=None, max_conversas: int = 20) -> int:
    """Trata conversas com estado 'ia' que têm mensagem nova ou acompanhamento vencido."""
    tratadas = 0
    conversas = _cur(conn).execute(
        """select c.* from radar.conversas c
           where c.estado = 'ia' and c.conversation_id is not null
             and (exists (select 1 from public.messages m where m.conversation_id = c.conversation_id
                          and m.created_at > coalesce(c.lida_ate, '-infinity') and m.sender_type in ('customer','agent'))
                  or c.proximo_followup_em <= %s)
           order by c.lida_ate nulls first limit %s for update skip locked""", (agora, max_conversas)).fetchall()
    for c in conversas:
        try:
            await _tratar(conn, comercial, c, agora, cliente_ia)
            tratadas += 1
        except Exception as e:                     # noqa: BLE001 — uma conversa não derruba as outras
            log.warning("conversa %s: %s", c["id"], e)
    return tratadas


def _novas(conn, c) -> list[dict]:
    return _cur(conn).execute(
        """select id, sender_type, content_type, content_text, created_at from public.messages
           where conversation_id = %s and created_at > coalesce(%s, '-infinity')
             and sender_type in ('customer','agent') order by created_at""",
        (c["conversation_id"], c["lida_ate"])).fetchall()


def _historico(conn, c) -> list[tuple[str, str]]:
    rows = conn.execute("""select sender_type, coalesce(content_text, '['||content_type||']') from public.messages
                           where conversation_id = %s and sender_type in ('customer','bot','agent')
                           order by created_at""", (c["conversation_id"],)).fetchall()
    return [("lead" if s == "customer" else "nos", t or "") for s, t in rows]


def _pessoa(conn, c) -> dict:
    """Com quem a IA fala: o lead do Radar ou, na LP, os dados do contexto (o worker não lê public.contacts)."""
    if c["origem"] == "lp":
        ctx = c["contexto"] or {}
        return {"id": None, "status": "lp", "motivo_descarte": None, "whatsapp": ctx.get("telefone"),
                "telefone": ctx.get("telefone"), "nome": ctx.get("nome") or ""}
    return _cur(conn).execute("select * from radar.leads where id = %s", (c["lead_id"],)).fetchone()


def _acao(intencao: str) -> str:
    """Fluxo antigo (sem triagem v2): a intenção da IA decide."""
    return {"pediu_humano": "pediu_humano", "nao_sei": "ia_sem_resposta", "sem_interesse": "sem_interesse",
            "qualificado": "qualificado"}.get(intencao, "seguir")


def _fuso(cfg_ag: dict) -> str:
    return cfg_ag.get("fuso") or "America/Sao_Paulo"


def _regras_v2(conn, c) -> dict | None:
    r = triagem.regras(conn, c["agency_id"])
    return r if triagem.v2_ativa(r, c["origem"]) else None


async def _tratar(conn, comercial, c, agora, cliente_ia) -> None:
    novas = _novas(conn, c)
    ultima = max((m["created_at"] for m in novas), default=c["lida_ate"])
    if any(m["sender_type"] == "agent" for m in novas):
        # um humano respondeu (inbox ou celular): a IA sai da conversa
        conn.execute("""update radar.conversas set estado = 'humano', motivo_fim = 'humano_assumiu',
                        assumida_em = %s, lida_ate = %s where id = %s""", (agora, ultima, c["id"]))
        return
    lp = c["origem"] == "lp"
    lead = _pessoa(conn, c)
    cfg_ag = _agencia(conn, c["agency_id"])
    do_lead = [m for m in novas if m["sender_type"] == "customer"]
    r2 = _regras_v2(conn, c)
    if r2 is not None:
        await _tratar_v2(conn, comercial, c, agora, cliente_ia, ultima, lead, cfg_ag, do_lead, r2)
        return

    if do_lead:
        if not lp:
            conn.execute("update radar.leads set status = 'respondeu' where id = %s and status = 'contatado'", (lead["id"],))
        tarefa = "Responda às últimas mensagens do lead."
        if any((m["content_type"] or "text") != "text" and not m["content_text"] for m in do_lead):
            tarefa += " O lead mandou áudio ou mídia que você não consegue ver: peça com gentileza para escrever."
    elif c["proximo_followup_em"] and c["proximo_followup_em"] <= agora:
        if c["followups_enviados"] >= followups(conn, c["agency_id"])[0]:
            conn.execute("""update radar.conversas set estado = 'encerrada', motivo_fim = 'sem_resposta',
                            proximo_followup_em = null where id = %s""", (c["id"],))
            if not lp:
                conn.execute("update radar.leads set sem_resposta = true where id = %s", (lead["id"],))
            return
        local = _agora_local(cfg_ag, agora)
        if not ((cfg_ag.get("janela_inicio") or JANELA_PADRAO[0]) <= local.time() < (cfg_ag.get("janela_fim") or JANELA_PADRAO[1])):
            return                                   # espera a janela abrir
        tarefa = "O lead ainda não respondeu. Escreva um acompanhamento curto, leve e sem pressão."
    else:
        return

    resp = await _chamar_ia(conn, comercial, c, lead, cfg_ag, agora, cliente_ia, ultima, tarefa, v2=None)
    if resp is None:
        return
    campos = {**(c["campos_coletados"] or {}), **{k: v for k, v in resp.campos.model_dump().items() if v}}
    conn.execute("update radar.conversas set campos_coletados = %s::jsonb where id = %s",
                 (json.dumps(campos), c["id"]))
    acao = _acao(resp.intencao)

    if resp.mensagem.strip():
        if acao in ("seguir", "qualificado"):
            await _reagir(comercial, c, do_lead, resp)
        if not await _enviar(conn, comercial, c, lead, cfg_ag, agora, resp.mensagem, campos, ultima):
            return

    if do_lead:
        conn.execute("""update radar.conversas set lida_ate = %s, followups_enviados = 0,
                        proximo_followup_em = %s where id = %s""",
                     (ultima, agora + _espera(followups(conn, c["agency_id"])[1], 0), c["id"]))
    else:
        n = c["followups_enviados"] + 1
        prox = agora + _espera(followups(conn, c["agency_id"])[1], n)   # depois do último: mesma espera e encerra
        conn.execute("""update radar.conversas set followups_enviados = %s, proximo_followup_em = %s
                        where id = %s""", (n, prox, c["id"]))

    if acao in ("qualificado", "pediu_humano", "ia_sem_resposta"):
        await _passar(conn, comercial, c, lead, acao, campos, resp.resumo)
    elif acao == "sem_interesse":
        _sem_interesse(conn, c, lead)


async def _chamar_ia(conn, comercial, c, lead, cfg_ag, agora, cliente_ia, ultima, tarefa, v2):
    """Chama a IA (com teto de custo) e registra o custo. IA indisponível = passa a conversa a um humano e devolve
    None. `v2` = (regras, fatos) na triagem v2 (o resumo ao SDR sai completo), None no fluxo antigo."""
    cfg = ia.carregar_config(conn, c["agency_id"])
    lp = c["origem"] == "lp"
    ficha = triagem.ficha_lp(c["contexto"]) if lp else (_ficha_v2(conn, lead) if v2 is not None else _ficha(conn, lead))
    try:
        ia.checar_orcamento(conn, cfg, c["agency_id"], agora)      # acima do teto o modelo nem é chamado
        resp, custo = await ia.responder(cfg, cfg_ag.get("nome_agencia") or "agência", ficha,
                                         _historico(conn, c), tarefa, cliente=cliente_ia, origem=c["origem"],
                                         v2=v2 is not None)
    except ia.IAIndisponivel as e:
        fim = "limite_custo" if isinstance(e, ia.LimiteCusto) else None
        if v2 is not None:
            await _passar_v2(conn, comercial, c, lead, v2[1], None, motivo="ia_sem_resposta", motivo_fim=fim,
                             resumo_ia=str(e))
        else:
            await _passar(conn, comercial, c, lead, "ia_sem_resposta", None, str(e), motivo_fim=fim)
        conn.execute("update radar.conversas set lida_ate = %s where id = %s", (ultima, c["id"]))
        return None
    conn.execute("""insert into radar.custos_api (agency_id, fonte, operacao, lead_id, qtd, valor, estimado)
                    values (%s, 'ia_radar', 'conversa', %s, 1, %s, false)""", (c["agency_id"], lead["id"], custo))
    return resp


async def _enviar(conn, comercial, c, lead, cfg_ag, agora, texto, campos, ultima, v2_fatos=None) -> bool:
    """Envia uma mensagem da conversa (resposta, toque, oferta) pela trava. Bloqueada = passa a um humano (False).
    Na triagem v2 também barra preço/piso com valor, desconto com número e comparação com concorrente."""
    trava = gate.checar(texto, gate.Contexto(
        lead_status=lead["status"], motivo_descarte=lead["motivo_descarte"], bloqueado=False,
        ultimo_contato_mesmo_canal=None, confirmado_recontato=True, disparo_confirmado=True,
        registrado_em_disparos_leads=True, agora_local_lead=_agora_local(cfg_ag, agora),
        janela_inicio=time(0), janela_fim=time(23, 59, 59),   # a janela é checada antes (resposta: sem janela)
        canal="whatsapp_nao_oficial", template_aprovado=False, primeira_mensagem=False))
    if v2_fatos is not None and triagem.assunto_proibido(texto):
        trava = gate.Resultado(False, [*trava.motivos, "assunto que a IA não trata (preço, desconto ou concorrente)"])
    if not trava:
        motivo = "a trava bloqueou a resposta da IA: " + "; ".join(trava.motivos)
        if v2_fatos is not None:
            await _passar_v2(conn, comercial, c, lead, v2_fatos, None, motivo="ia_sem_resposta", resumo_ia=motivo)
        else:
            await _passar(conn, comercial, c, lead, "ia_sem_resposta", campos, motivo)
        conn.execute("update radar.conversas set lida_ate = %s where id = %s", (ultima, c["id"]))
        return False
    # "digitando..." pelo tempo do texto antes de enviar (falha de presença não segura a resposta)
    await toque_humano.digitar(comercial, agency_id=c["agency_id"], conversation_id=c["conversation_id"], texto=texto)
    r = await comercial.enviar(agency_id=c["agency_id"], lead_id=lead["id"],
                               whatsapp_config_id=c["whatsapp_config_id"], telefone=lead["whatsapp"] or lead["telefone"],
                               nome=lead["nome"], texto=texto, primeira=False,
                               **({"conversa_id": str(c["id"])} if c["origem"] == "lp" else {}))
    if not r.ok:
        raise RuntimeError(f"envio falhou: {r.motivo}")
    return True


async def _reagir(comercial, c, do_lead, resp) -> None:
    """Reação da IA à última mensagem do lead (só resposta a mensagem nova; filtro e ritmo em toque_humano/COMERCIAL)."""
    if do_lead:
        await toque_humano.reagir(comercial, agency_id=c["agency_id"], conversation_id=c["conversation_id"],
                                  mensagens_do_lead=do_lead, reacao=resp.reacao, intencao=resp.intencao)


def _sem_interesse(conn, c, lead) -> None:
    # vale só para este contato: a empresa decide se e quando reabordar (decisão do Philip, 20/10)
    conn.execute("""update radar.conversas set estado = 'encerrada', motivo_fim = 'sem_interesse',
                    proximo_followup_em = null where id = %s""", (c["id"],))
    if lead["id"]:
        conn.execute("""update radar.leads set status = 'descartado', motivo_descarte = 'outro',
                        motivo_descarte_texto = 'Sem interesse (respondeu à IA)' where id = %s""", (lead["id"],))


async def _passar(conn, comercial, c, lead, motivo, campos, resumo, motivo_fim=None) -> None:
    await comercial.passar(agency_id=c["agency_id"], lead_id=lead["id"], conversation_id=c["conversation_id"],
                           motivo=motivo, campos=campos, resumo=resumo)
    conn.execute("""update radar.conversas set estado = 'humano', motivo_fim = %s, proximo_followup_em = null
                    where id = %s""", (motivo_fim or motivo, c["id"]))
    if motivo == "qualificado" and lead["id"]:
        conn.execute("update radar.leads set status = 'qualificado' where id = %s", (lead["id"],))


# ── triagem v2: portões, cadência de 5 toques, esteira e agenda ──────────
async def _tratar_v2(conn, comercial, c, agora, cliente_ia, ultima, lead, cfg_ag, do_lead, r2) -> None:
    lp = c["origem"] == "lp"
    fuso = _fuso(cfg_ag)
    cad = triagem.cadencia(conn, c["agency_id"])
    fatos = dict(((c["nota_detalhe"] or {}).get("fatos")) or {})
    if not lp:
        # Radar: o que a ficha já sabe (cidade do Maps, segmento, abertura do CNPJ) entra como fato conhecido;
        # o que o lead disse na conversa vale por cima
        fatos = triagem.juntar_fatos(triagem.fatos_da_ficha(lead, _empresa(conn, lead["id"])), fatos)
    etapa = c["etapa"]
    toque = None

    if do_lead:
        if not lp:
            conn.execute("update radar.leads set status = 'respondeu' where id = %s and status = 'contatado'", (lead["id"],))
        base = "Responda às últimas mensagens do lead."
        if any((m["content_type"] or "text") != "text" and not m["content_text"] for m in do_lead):
            base += " O lead mandou áudio ou mídia que você não consegue ver: peça com gentileza para escrever."
        if etapa == "esteira":
            # respondeu na esteira: a qualificação volta de onde parou; o prazo antigo (> 60 d) é perguntado de novo
            if fatos.get("prazo_inicio") in r2["timing"]["esteira"]:
                fatos["prazo_inicio_anterior"] = fatos.pop("prazo_inicio")
            etapa = "qualificando"
            conn.execute("""update radar.conversas set etapa = 'qualificando', esteira_desde = null, esteira_enviados = 0,
                            nota_detalhe = jsonb_set(coalesce(nota_detalhe, '{}'), '{fatos}', %s::jsonb)
                            where id = %s""", (json.dumps(fatos, default=str), c["id"]))
            base += " O lead voltou a responder depois de um tempo: retome a conversa de onde parou."
    elif c["proximo_followup_em"] and c["proximo_followup_em"] <= agora:
        if not triagem.na_janela(agora, fuso, cad["janela"]):
            # só seg-sex 9h-18h (fuso da agência): empurra para a próxima abertura
            conn.execute("update radar.conversas set proximo_followup_em = %s where id = %s",
                         (triagem.proxima_janela(agora, fuso, cad["janela"]), c["id"]))
            return
        if etapa == "esteira":
            await _toque_esteira(conn, comercial, c, agora, cliente_ia, ultima, lead, cfg_ag, cad, fatos, r2)
            return
        n = c["followups_enviados"]
        if n >= len(cad["toques"]) - 1:
            await _fim_cadencia(conn, comercial, c, agora, lead, cfg_ag, cad, fatos, r2)
            return
        toque = n + 1
        tipo = cad["toques"][toque]["tipo"]
        base = cad["tipos"].get(tipo) or "O lead ainda não respondeu. Escreva um acompanhamento curto e leve."
        base = base.replace("{segmento}", str(fatos.get("segmento") or "do lead"))
    else:
        return

    if etapa == "oferta":
        ops = (c["agenda"] or {}).get("opcoes") or []
        tarefa = (base + " Na última mensagem oferecemos 2 horários com o consultor: "
                  + " e ".join(f"opção {i + 1} = {o.get('rotulo')}" for i, o in enumerate(ops))
                  + ". Registre em escolha_horario qual o lead escolheu (\"nenhuma\" se nenhum servir). "
                  "Não ofereça outros horários.")
    else:
        cobertos = triagem.temas_do_formulario((c["contexto"] or {}).get("respostas") or []) if lp else {}
        tarefa = triagem.tarefa(fatos, r2, triagem.textos(conn, c["agency_id"], "triagem_roteiro",
                                                          triagem.ROTEIRO_PADRAO), cobertos, base)

    resp = await _chamar_ia(conn, comercial, c, lead, cfg_ag, agora, cliente_ia, ultima, tarefa, v2=(r2, fatos))
    if resp is None:
        return
    campos = {**(c["campos_coletados"] or {}), **{k: v for k, v in resp.campos.model_dump().items() if v}}
    fatos = triagem.juntar_fatos(fatos, resp.fatos.model_dump())
    res = triagem.avaliar(fatos, r2)
    decisao = "passa_direto" if etapa == "oferta" else res.classificacao       # na oferta a decisão já saiu
    conn.execute("""update radar.conversas set campos_coletados = %s::jsonb, nota_detalhe = %s::jsonb, decisao = %s
                    where id = %s""", (json.dumps(campos), json.dumps(res.detalhe(fatos), default=str), decisao, c["id"]))

    # o que o lead pediu explicitamente vale antes dos portões
    if resp.intencao in ("pediu_humano", "nao_sei"):
        if resp.mensagem.strip() and not await _enviar(conn, comercial, c, lead, cfg_ag, agora, resp.mensagem,
                                                       campos, ultima, fatos):
            return
        await _passar_v2(conn, comercial, c, lead, fatos, res,
                         motivo="pediu_humano" if resp.intencao == "pediu_humano" else "ia_sem_resposta",
                         resumo_ia=resp.resumo)
        conn.execute("update radar.conversas set lida_ate = %s where id = %s", (ultima, c["id"]))
        return
    if resp.intencao == "sem_interesse":
        if resp.mensagem.strip() and not await _enviar(conn, comercial, c, lead, cfg_ag, agora, resp.mensagem,
                                                       campos, ultima, fatos):
            return
        _sem_interesse(conn, c, lead)
        conn.execute("update radar.conversas set lida_ate = %s where id = %s", (ultima, c["id"]))
        return

    conn.execute("update radar.conversas set lida_ate = %s where id = %s", (ultima, c["id"]))
    nome = cfg_ag.get("nome_agencia") or "agência"
    ctx = (conn, comercial, c, lead, cfg_ag, agora, fatos, res, campos, ultima)

    if etapa == "oferta" and do_lead and resp.escolha_horario in ("opcao_1", "opcao_2"):
        await _reagir(comercial, c, do_lead, resp)
        await _agendar(*ctx, int(resp.escolha_horario[-1]) - 1)
        return
    if etapa == "oferta" and do_lead and resp.escolha_horario == "nenhuma":
        if await _enviar(conn, comercial, c, lead, cfg_ag, agora,
                         triagem.mensagem(conn, c["agency_id"], c["origem"], "passar_sem_horario", nome), campos, ultima, fatos):
            await _passar_v2(conn, comercial, c, lead, fatos, res, classificacao="passa_direto",
                             extras=["Agenda: nenhum dos 2 horários oferecidos serviu ao lead; combinar outro."],
                             resumo_ia=resp.resumo)
        return
    if etapa == "qualificando" and do_lead and res.classificacao != "em_andamento":
        await _decidir(*ctx, resp.resumo)
        return

    # segue a conversa: a mensagem da IA sai e a cadência anda
    if resp.mensagem.strip():
        await _reagir(comercial, c, do_lead, resp)
    if resp.mensagem.strip() and not await _enviar(conn, comercial, c, lead, cfg_ag, agora, resp.mensagem,
                                                   campos, ultima, fatos):
        return
    if toque is None:
        _reiniciar_cadencia(conn, c["id"], cad, agora, _fuso(cfg_ag))
    else:
        _avancar_cadencia(conn, c, cad, toque, agora, _fuso(cfg_ag))


def _reiniciar_cadencia(conn, conversa_id, cad, agora, fuso) -> None:
    """Mensagem nossa depois de uma resposta do lead = toque 1 de uma cadência nova."""
    prox = triagem.vencimento_toque(cad, 1, agora, agora, fuso) if len(cad["toques"]) > 1 else \
        triagem.proxima_janela(agora + timedelta(hours=cad["encerrar_apos_horas"]), fuso, cad["janela"])
    conn.execute("""update radar.conversas set cadencia_inicio = %s, followups_enviados = 0, proximo_followup_em = %s
                    where id = %s""", (agora, prox, conversa_id))


def _avancar_cadencia(conn, c, cad, toque, agora, fuso) -> None:
    inicio = c["cadencia_inicio"] or c["iniciada_em"] or agora
    if toque + 1 < len(cad["toques"]):
        prox = triagem.vencimento_toque(cad, toque + 1, inicio, agora, fuso)
    else:                                          # depois do último toque: espera e decide (esteira ou encerra)
        prox = triagem.proxima_janela(agora + timedelta(hours=cad["encerrar_apos_horas"]), fuso, cad["janela"])
    conn.execute("update radar.conversas set followups_enviados = %s, proximo_followup_em = %s where id = %s",
                 (toque, prox, c["id"]))


async def _decidir(conn, comercial, c, lead, cfg_ag, agora, fatos, res, campos, ultima, resumo_ia) -> None:
    nome = cfg_ag.get("nome_agencia") or "agência"
    cl = res.classificacao
    if cl == "passa_direto":
        await _oferecer(conn, comercial, c, lead, cfg_ag, agora, fatos, res, campos, ultima, tentativas=0,
                        resumo_ia=resumo_ia)
        return
    tipo = {"passa_alerta": "passar", "redireciona": "passar", "esteira": "esteira", "desqualifica": "descartar"}[cl]
    if not await _enviar(conn, comercial, c, lead, cfg_ag, agora,
                         triagem.mensagem(conn, c["agency_id"], c["origem"], tipo, nome), campos, ultima, fatos):
        return
    if cl in ("passa_alerta", "redireciona"):
        await _passar_v2(conn, comercial, c, lead, fatos, res, classificacao=cl, alertas=res.alertas,
                         resumo_ia=resumo_ia)
    elif cl == "esteira":
        _entrar_esteira(conn, c, agora, triagem.cadencia(conn, c["agency_id"]), _fuso(cfg_ag))
    else:
        await _desqualificar(conn, comercial, c, lead, res)


async def _oferecer(conn, comercial, c, lead, cfg_ag, agora, fatos, res, campos, ultima, tentativas,
                    resumo_ia=None) -> None:
    """passa_direto: 2 horários do Google do SDR (rota /horarios). Sem Google, sem horário ou falha = passa ao SDR
    sem oferecer."""
    nome = cfg_ag.get("nome_agencia") or "agência"
    try:
        h = await comercial.horarios(agency_id=c["agency_id"], conversa_id=str(c["id"]),
                                     conversation_id=c["conversation_id"], lead_id=lead["id"]) or {}
    except Exception as e:                         # noqa: BLE001 — rede/5xx: não oferece, não trava o lead
        log.warning("horarios da conversa %s falhou: %s", c["id"], e)
        h = {"ok": False, "motivo": f"erro ({str(e)[:100]})"}
    opcoes = [o for o in (h.get("opcoes") or []) if isinstance(o, dict) and o.get("inicio") and o.get("rotulo")]
    if not (h.get("ok") and h.get("sdr_id") and len(opcoes) >= 2):
        motivo = h.get("motivo") or "sem_horario"
        if await _enviar(conn, comercial, c, lead, cfg_ag, agora,
                         triagem.mensagem(conn, c["agency_id"], c["origem"], "passar_sem_horario", nome), campos, ultima, fatos):
            await _passar_v2(conn, comercial, c, lead, fatos, res, classificacao="passa_direto",
                             extras=[f"Agenda: horário não oferecido ({motivo}); combinar com o lead."],
                             resumo_ia=resumo_ia)
        return
    opcoes = opcoes[:2]
    tipo = "oferta" if tentativas == 0 else "oferta_nova"
    texto = triagem.mensagem(conn, c["agency_id"], c["origem"], tipo, nome,
                             opcao_1=opcoes[0]["rotulo"], opcao_2=opcoes[1]["rotulo"])
    if not await _enviar(conn, comercial, c, lead, cfg_ag, agora, texto, campos, ultima, fatos):
        return
    agenda = {"sdr_id": h["sdr_id"], "opcoes": opcoes, "tentativas": tentativas + 1}
    conn.execute("update radar.conversas set etapa = 'oferta', agenda = %s::jsonb, decisao = 'passa_direto' where id = %s",
                 (json.dumps(agenda, default=str), c["id"]))
    _reiniciar_cadencia(conn, c["id"], triagem.cadencia(conn, c["agency_id"]), agora, _fuso(cfg_ag))


async def _agendar(conn, comercial, c, lead, cfg_ag, agora, fatos, res, campos, ultima, indice) -> None:
    nome = cfg_ag.get("nome_agencia") or "agência"
    agenda = dict(c["agenda"] or {})
    op = agenda["opcoes"][indice]
    try:
        a = await comercial.agendar(agency_id=c["agency_id"], conversa_id=str(c["id"]),
                                    conversation_id=c["conversation_id"], lead_id=lead["id"], sdr_id=agenda["sdr_id"],
                                    inicio=op["inicio"], fim=op.get("fim") or op["inicio"],
                                    idempotency_key=f"radar-agendar-{c['id']}-{op['inicio']}") or {}
    except Exception as e:                         # noqa: BLE001
        log.warning("agendar da conversa %s falhou: %s", c["id"], e)
        a = {"ok": False, "motivo": f"erro ({str(e)[:100]})"}
    if a.get("ok"):
        escolhido = {"sdr_id": agenda["sdr_id"], "inicio": a.get("inicio") or op["inicio"],
                     "fim": a.get("fim") or op.get("fim"), "rotulo": op["rotulo"], "evento_id": a.get("evento_id"),
                     "lead_call_id": a.get("lead_call_id"), "meet_url": a.get("meet_url")}
        conn.execute("update radar.conversas set agenda = %s::jsonb where id = %s",
                     (json.dumps({**agenda, "escolhido": escolhido}, default=str), c["id"]))
        if await _enviar(conn, comercial, c, lead, cfg_ag, agora,
                         triagem.mensagem(conn, c["agency_id"], c["origem"], "confirmacao", nome, horario=op["rotulo"]),
                         campos, ultima, fatos):
            await _passar_v2(conn, comercial, c, lead, fatos, res, classificacao="passa_direto", agendamento=escolhido)
        return
    if a.get("motivo") == "horario_indisponivel" and agenda.get("tentativas", 1) < 2:
        await _oferecer(conn, comercial, c, lead, cfg_ag, agora, fatos, res, campos, ultima,
                        tentativas=agenda.get("tentativas", 1))
        return
    if await _enviar(conn, comercial, c, lead, cfg_ag, agora,
                     triagem.mensagem(conn, c["agency_id"], c["origem"], "passar_sem_horario", nome), campos, ultima, fatos):
        await _passar_v2(conn, comercial, c, lead, fatos, res, classificacao="passa_direto",
                         extras=[f"Agenda: o lead escolheu {op['rotulo']}, mas não deu para agendar "
                                 f"({a.get('motivo') or 'erro'}); combinar com o lead."])


def _entrar_esteira(conn, c, agora, cad, fuso) -> None:
    prox = triagem.proxima_janela(agora + timedelta(days=cad["esteira"]["intervalo_dias"]), fuso, cad["janela"])
    conn.execute("""update radar.conversas set etapa = 'esteira', decisao = 'esteira', esteira_desde = %s,
                    esteira_enviados = 0, followups_enviados = 0, proximo_followup_em = %s where id = %s""",
                 (agora, prox, c["id"]))


async def _toque_esteira(conn, comercial, c, agora, cliente_ia, ultima, lead, cfg_ag, cad, fatos, r2) -> None:
    """1 mensagem de valor a cada 30 dias por 3 meses; depois da última, sem resposta = encerra 'sem_resposta'."""
    e, fuso = cad["esteira"], _fuso(cfg_ag)
    n = c["esteira_enviados"]
    if n >= e["mensagens"]:
        _encerrar_sem_resposta(conn, c, lead)
        return
    tarefa = cad["tipos"]["esteira"].replace("{segmento}", str(fatos.get("segmento") or "do lead"))
    resp = await _chamar_ia(conn, comercial, c, lead, cfg_ag, agora, cliente_ia, ultima, tarefa, v2=(r2, fatos))
    if resp is None:
        return
    if resp.mensagem.strip() and not await _enviar(conn, comercial, c, lead, cfg_ag, agora, resp.mensagem,
                                                   c["campos_coletados"], ultima, fatos):
        return                                     # mensagem vazia: conta o toque (não chama a IA de novo a cada rodada)
    n += 1
    desde = c["esteira_desde"] or agora
    if n < e["mensagens"]:
        prox = triagem.proxima_janela(desde + timedelta(days=e["intervalo_dias"] * (n + 1)), fuso, cad["janela"])
    else:
        prox = triagem.proxima_janela(agora + timedelta(days=e["espera_final_dias"]), fuso, cad["janela"])
    conn.execute("update radar.conversas set esteira_enviados = %s, proximo_followup_em = %s where id = %s",
                 (n, prox, c["id"]))


async def _fim_cadencia(conn, comercial, c, agora, lead, cfg_ag, cad, fatos, r2) -> None:
    """5 toques sem resposta. Na oferta o lead já passou nos portões: vai ao SDR sem horário. Senão: esteira se o
    bom perfil já é conhecido, ou encerra."""
    res = triagem.avaliar(fatos, r2)
    if c["etapa"] == "oferta":
        await _passar_v2(conn, comercial, c, lead, fatos, res, classificacao="passa_direto",
                         extras=["Agenda: o lead não respondeu à oferta de horários; combinar com o lead."])
        return
    if triagem.bom_perfil(res):
        _entrar_esteira(conn, c, agora, cad, _fuso(cfg_ag))
        return
    _encerrar_sem_resposta(conn, c, lead)


def _encerrar_sem_resposta(conn, c, lead) -> None:
    conn.execute("""update radar.conversas set estado = 'encerrada', motivo_fim = 'sem_resposta',
                    proximo_followup_em = null where id = %s""", (c["id"],))
    if lead["id"]:
        conn.execute("update radar.leads set sem_resposta = true where id = %s", (lead["id"],))


def _dados_pessoa(c, lead) -> dict:
    ctx = c["contexto"] or {}
    if c["origem"] == "lp":
        return {"nome": ctx.get("nome"), "empresa": ctx.get("empresa"), "telefone": ctx.get("telefone")}
    return {"nome": ctx.get("nome") or lead.get("nome"), "empresa": lead.get("nome"),
            "telefone": lead.get("whatsapp") or lead.get("telefone")}


async def _passar_v2(conn, comercial, c, lead, fatos, res, motivo="qualificado", classificacao=None, alertas=None,
                     agendamento=None, extras=None, resumo_ia=None, motivo_fim=None) -> None:
    """Passa ao SDR com o resumo completo (todos os campos do contrato) e a classificação."""
    url = getattr(comercial, "url", "") or ""
    link = f"{url}/inbox?c={c['conversation_id']}" if c["conversation_id"] else None
    resumo = triagem.resumo_sdr(_dados_pessoa(c, lead), fatos, res, link, agendamento, resumo_ia)
    if extras:
        resumo += "\n" + "\n".join(extras)
    campos = {k: (", ".join(map(str, v)) if isinstance(v, list) else v)
              for k, v in (fatos or {}).items() if triagem.conhecido(v)}
    await comercial.passar(agency_id=c["agency_id"], lead_id=lead["id"], conversation_id=c["conversation_id"],
                           motivo=motivo, campos=campos, resumo=resumo,
                           **({"classificacao": classificacao, "alertas": alertas or [], "agendamento": agendamento}
                              if classificacao else {}))
    conn.execute("""update radar.conversas set estado = 'humano', motivo_fim = %s, proximo_followup_em = null
                    where id = %s""", (motivo_fim or motivo, c["id"]))
    if motivo == "qualificado" and lead["id"]:
        conn.execute("update radar.leads set status = 'qualificado' where id = %s", (lead["id"],))


async def _desqualificar(conn, comercial, c, lead, res) -> None:
    """Desqualifica: encerra com a justificativa (LP: o COMERCIAL etiqueta "Fora do perfil")."""
    motivo = f"Desqualificado: {res.justificativa}"[:500]
    if c["origem"] == "lp":
        await comercial.descartar(agency_id=c["agency_id"], conversa_id=str(c["id"]), motivo=motivo)
    conn.execute("""update radar.conversas set estado = 'encerrada', motivo_fim = 'desqualificado',
                    proximo_followup_em = null where id = %s""", (c["id"],))
    if lead["id"]:
        conn.execute("""update radar.leads set status = 'descartado', motivo_descarte = 'outro',
                        motivo_descarte_texto = %s where id = %s""", (motivo, lead["id"]))
