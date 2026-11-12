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
a mesma janela, limites e trava; o registro prévio fica em conversas.contexto.primeira. A cada resposta, a
nota sai de radar.triagem (fatos da IA + respostas da LP + radar.criterios_triagem), não da IA. Sem
critérios ativos para a agência/origem, vale o fluxo de antes (intenção da IA).
"""

import json
import logging
import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from radar import gate, ia, triagem
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
    lim_hora = min((disparo or {}).get("limite_hora") or 10**6, cfg.get("limite_hora") or 10**6)
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
                 iniciada_em, lida_ate, proximo_followup_em, followups_enviados)
           values (%s,%s,'ia',%s,%s,%s,%s,%s,%s,0)
           on conflict (lead_id) do update set estado = 'ia', conversation_id = excluded.conversation_id,
                 contact_id = excluded.contact_id, whatsapp_config_id = excluded.whatsapp_config_id,
                 iniciada_em = excluded.iniciada_em, lida_ate = excluded.lida_ate,
                 proximo_followup_em = excluded.proximo_followup_em, followups_enviados = 0, motivo_fim = null""",
        (d["agency_id"], lead["id"], r.conversation_id, r.contact_id, conexao, agora, agora,
         agora + _espera(followups(conn, d["agency_id"])[1], 0)))
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
                        followups_enviados = 0, whatsapp_config_id = %s,
                        conversation_id = coalesce(conversation_id, %s), contact_id = coalesce(contact_id, %s)
                    where id = %s""",
                 (agora, agora, agora + _espera(followups(conn, c["agency_id"])[1], 0), conexao, r.conversation_id,
                  r.contact_id, c["id"]))
    return True


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
        """select sender_type, content_type, content_text, created_at from public.messages
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


def _acao(intencao: str, resultado: "triagem.Resultado | None") -> str:
    """O que fazer depois da resposta. Com critérios, a decisão sai da nota; a IA só sinaliza o que o lead
    pediu explicitamente (pessoa, sem interesse) ou que não sabe seguir."""
    if intencao == "pediu_humano":
        return "pediu_humano"
    if intencao == "nao_sei":
        return "ia_sem_resposta"
    if intencao == "sem_interesse":
        return "sem_interesse"
    if resultado is None:
        return "qualificado" if intencao == "qualificado" else "seguir"
    return {"passar": "qualificado", "descartar": "descartar"}.get(resultado.decisao, "seguir")


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

    cfg = ia.carregar_config(conn, c["agency_id"])
    ficha = triagem.ficha_lp(c["contexto"]) if lp else _ficha(conn, lead)
    try:
        ia.checar_orcamento(conn, cfg, c["agency_id"], agora)      # acima do teto o modelo nem é chamado
        resp, custo = await ia.responder(cfg, cfg_ag.get("nome_agencia") or "agência", ficha,
                                         _historico(conn, c), tarefa, cliente=cliente_ia, origem=c["origem"])
    except ia.IAIndisponivel as e:
        fim = "limite_custo" if isinstance(e, ia.LimiteCusto) else None
        await _passar(conn, comercial, c, lead, "ia_sem_resposta", None, str(e), motivo_fim=fim)
        conn.execute("update radar.conversas set lida_ate = %s where id = %s", (ultima, c["id"]))
        return
    conn.execute("""insert into radar.custos_api (agency_id, fonte, operacao, lead_id, qtd, valor, estimado)
                    values (%s, 'ia_radar', 'conversa', %s, 1, %s, false)""", (c["agency_id"], lead["id"], custo))
    campos = {**(c["campos_coletados"] or {}), **{k: v for k, v in resp.campos.model_dump().items() if v}}
    conn.execute("update radar.conversas set campos_coletados = %s::jsonb where id = %s",
                 (json.dumps(campos), c["id"]))

    # nota determinística, recalculada a cada resposta (só com critérios ativos para a agência/origem)
    resultado = None
    crit = triagem.carregar_criterios(conn, c["agency_id"], c["origem"])
    if crit:
        fatos = triagem.juntar_fatos((c["nota_detalhe"] or {}).get("fatos"), resp.fatos.model_dump())
        resultado = triagem.calcular(crit, fatos, (c["contexto"] or {}).get("respostas") or [])
        conn.execute("""update radar.conversas set nota = %s, nota_detalhe = %s::jsonb, decisao = %s,
                        versao_criterios = %s where id = %s""",
                     (resultado.nota, json.dumps(resultado.detalhe, default=str), resultado.decisao,
                      crit.versao, c["id"]))
    acao = _acao(resp.intencao, resultado)

    mensagem = resp.mensagem
    if resultado and acao in ("qualificado", "descartar"):
        mensagem = triagem.mensagem(conn, c["agency_id"], c["origem"], "passar" if acao == "qualificado" else "descartar",
                                    cfg_ag.get("nome_agencia") or "agência") or mensagem
    if mensagem.strip():
        trava = gate.checar(mensagem, gate.Contexto(
            lead_status=lead["status"], motivo_descarte=lead["motivo_descarte"], bloqueado=False,
            ultimo_contato_mesmo_canal=None, confirmado_recontato=True, disparo_confirmado=True,
            registrado_em_disparos_leads=True, agora_local_lead=_agora_local(cfg_ag, agora),
            janela_inicio=time(0), janela_fim=time(23, 59, 59),   # resposta a quem escreveu: sem janela
            canal="whatsapp_nao_oficial", template_aprovado=False, primeira_mensagem=False))
        if not trava:
            await _passar(conn, comercial, c, lead, "ia_sem_resposta", campos,
                          "a trava bloqueou a resposta da IA: " + "; ".join(trava.motivos), resultado=resultado)
            conn.execute("update radar.conversas set lida_ate = %s where id = %s", (ultima, c["id"]))
            return
        r = await comercial.enviar(agency_id=c["agency_id"], lead_id=lead["id"],
                                   whatsapp_config_id=c["whatsapp_config_id"], telefone=lead["whatsapp"] or lead["telefone"],
                                   nome=lead["nome"], texto=mensagem, primeira=False,
                                   **({"conversa_id": str(c["id"])} if lp else {}))
        if not r.ok:
            raise RuntimeError(f"envio falhou: {r.motivo}")

    if do_lead:
        conn.execute("""update radar.conversas set lida_ate = %s, followups_enviados = 0,
                        proximo_followup_em = %s where id = %s""",
                     (ultima, agora + _espera(followups(conn, c["agency_id"])[1], 0), c["id"]))
    else:
        n = c["followups_enviados"] + 1
        prox = agora + _espera(followups(conn, c["agency_id"])[1], n)   # depois do último: mesma espera e encerra
        conn.execute("""update radar.conversas set followups_enviados = %s, proximo_followup_em = %s
                        where id = %s""", (n, prox, c["id"]))

    if acao == "qualificado":
        resumo = resp.resumo + (f" (nota da triagem: {resultado.nota}/100)" if resultado else "")
        await _passar(conn, comercial, c, lead, "qualificado", campos, resumo, resultado=resultado)
    elif acao in ("pediu_humano", "ia_sem_resposta"):
        await _passar(conn, comercial, c, lead, acao, campos, resp.resumo, resultado=resultado)
    elif acao == "descartar":
        await _descartar(conn, comercial, c, lead, resultado)
    elif acao == "sem_interesse":
        # vale só para este contato: a empresa decide se e quando reabordar (decisão do Philip, 20/10)
        conn.execute("""update radar.conversas set estado = 'encerrada', motivo_fim = 'sem_interesse',
                        proximo_followup_em = null where id = %s""", (c["id"],))
        if not lp:
            conn.execute("""update radar.leads set status = 'descartado', motivo_descarte = 'outro',
                            motivo_descarte_texto = 'Sem interesse (respondeu à IA)' where id = %s""", (lead["id"],))


async def _passar(conn, comercial, c, lead, motivo, campos, resumo, motivo_fim=None, resultado=None) -> None:
    if c["origem"] == "lp" and resultado is not None:
        # LP: o COMERCIAL aceita os fatos dos critérios como campos (valores simples)
        campos = {**(campos or {}), **{k: v for k, v in resultado.detalhe["fatos"].items() if v is not None}}
    await comercial.passar(agency_id=c["agency_id"], lead_id=lead["id"], conversation_id=c["conversation_id"],
                           motivo=motivo, campos=campos, resumo=resumo)
    conn.execute("""update radar.conversas set estado = 'humano', motivo_fim = %s, proximo_followup_em = null
                    where id = %s""", (motivo_fim or motivo, c["id"]))
    if motivo == "qualificado" and lead["id"]:
        conn.execute("update radar.leads set status = 'qualificado' where id = %s", (lead["id"],))


async def _descartar(conn, comercial, c, lead, resultado) -> None:
    """Nota abaixo do corte de descarte: encerra com motivo (LP: o COMERCIAL etiqueta "Fora do perfil")."""
    motivo = f"Fora do perfil (nota {resultado.nota}/100, corte {resultado.detalhe['corte_descartar']})"
    if c["origem"] == "lp":
        await comercial.descartar(agency_id=c["agency_id"], conversa_id=str(c["id"]), motivo=motivo)
    conn.execute("""update radar.conversas set estado = 'encerrada', motivo_fim = 'fora_do_perfil',
                    proximo_followup_em = null where id = %s""", (c["id"],))
    if lead["id"]:
        conn.execute("""update radar.leads set status = 'descartado', motivo_descarte = 'outro',
                        motivo_descarte_texto = %s where id = %s""", (motivo, lead["id"]))
