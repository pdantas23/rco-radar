"""Fase 3 (despachante e conversador) contra Postgres real, com COMERCIAL e IA simulados."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from radar import conversa, ia
from radar.chaves import cifrar, decifrar
from radar.comercial import ResultadoEnvio
from tests.conftest import AGENCIA, CONTA_MESTRE, DONO

CONEXAO = "00000000-0000-0000-0000-0000000000c9"

# 13:00 UTC = 10:00 em São Paulo (dentro da janela 8h–20h)
AGORA = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)


class FakeComercial:
    """Imita as rotas internas: cria a conversa e grava a mensagem 'bot' no inbox."""

    def __init__(self, conn, falha=None):
        self.conn, self.falha = conn, falha
        self.envios, self.passagens = [], []

    async def enviar(self, *, agency_id, lead_id, whatsapp_config_id, telefone, nome, texto, primeira,
                     idempotency_key=None):
        self.envios.append({"lead_id": str(lead_id), "texto": texto, "primeira": primeira})
        if self.falha:
            return ResultadoEnvio(False, motivo=self.falha)
        conv = self.conn.execute("select conversation_id, contact_id from radar.conversas where lead_id = %s",
                                 (lead_id,)).fetchone()
        if conv and conv[0]:
            conv_id, contato = conv
        else:
            contato = self.conn.execute("insert into public.contacts default values returning id").fetchone()[0]
            conv_id = self.conn.execute("insert into public.conversations (contact_id) values (%s) returning id",
                                        (contato,)).fetchone()[0]
        self.conn.execute("""insert into public.messages (conversation_id, sender_type, content_text, created_at)
                             values (%s, 'bot', %s, %s)""", (conv_id, texto, AGORA))
        return ResultadoEnvio(True, str(conv_id), str(contato), "m1")

    async def passar(self, **kw):
        self.passagens.append(kw)
        return {"ok": True}


class FakeIA:
    def __init__(self, *respostas):
        self.respostas, self.chamadas = list(respostas), []
        self.messages = self

    async def parse(self, **kw):
        self.chamadas.append(kw)
        return SimpleNamespace(parsed_output=self.respostas.pop(0), stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


def resposta(mensagem="Legal! E hoje vocês fazem alguma divulgação?", intencao="continuar", **campos):
    return ia.RespostaIA(mensagem=mensagem, campos=ia.CamposColetados(**campos), intencao=intencao,
                         resumo="Dono, agenda cheia só no fim de semana.")


def preparar(conn, n_leads=3, intervalo=90, limite_hora=40, confirmado=True, ia_ligada=True):
    conn.execute("truncate radar.disparos_leads, radar.disparos, radar.abordagens, radar.conversas, radar.bloqueios cascade")
    conn.execute("truncate public.messages, public.conversations, public.agent_configs cascade")
    conn.execute("insert into public.agent_configs (agency_id, radar_agent_enabled) values (%s, %s)", (AGENCIA, ia_ligada))
    conn.execute("""insert into public.whatsapp_config (id, account_id) values (%s, %s) on conflict do nothing""",
                 (CONEXAO, CONTA_MESTRE))
    conn.execute("update radar.agencias set whatsapp_config_id = %s where agency_id = %s", (CONEXAO, AGENCIA))
    leads = []
    for i in range(n_leads):
        leads.append(conn.execute(
            """insert into radar.leads (agency_id, place_id, nome, endereco, cidade, estado, link_maps, fonte_maps,
                     telefone, whatsapp, tem_whatsapp_provavel, nota, status, pontuacao, classificacao)
               values (%s, %s, %s, 'Rua A', 'Teresina', 'PI', 'l', 'f', %s, %s, true, 4.6, 'pontuado', 90, 'quente')
               returning id""",
            (AGENCIA, f"p{i}", f"Barbearia {i}", f"+55869999900{i:02d}", f"+55869999900{i:02d}")).fetchone()[0])
    ab = conn.execute("""insert into radar.abordagens (agency_id, nome, canal, texto)
                         values (%s, 'sem anúncio', 'whatsapp_nao_oficial',
                                 'Oi, falo com o responsável pela {nome_negocio}? Vi que {ponto_da_ficha}.')
                         returning id""", (AGENCIA,)).fetchone()[0]
    disp = conn.execute(
        """insert into radar.disparos (agency_id, usuario_id, abordagem_id, filtro, qtd, intervalo_seg, limite_hora,
                 confirmado_em, status) values (%s, %s, %s, '{}', %s, %s, %s, %s, 'fila') returning id""",
        (AGENCIA, DONO, ab, n_leads, intervalo, limite_hora, AGORA if confirmado else None)).fetchone()[0]
    for lid in leads:
        conn.execute("insert into radar.disparos_leads (agency_id, disparo_id, lead_id) values (%s, %s, %s)",
                     (AGENCIA, disp, lid))
    return leads, disp


def lead_responde(conn, lead_id, texto, quando, autor="customer"):
    conv = conn.execute("select conversation_id from radar.conversas where lead_id = %s", (lead_id,)).fetchone()[0]
    conn.execute("""insert into public.messages (conversation_id, sender_type, content_text, created_at)
                    values (%s, %s, %s, %s)""", (conv, autor, texto, quando))


# ── despachante ──────────────────────────────────────────────────────────
async def test_um_por_vez_no_intervalo(conn):
    leads, _ = preparar(conn)
    com = FakeComercial(conn)
    assert await conversa.despachar(conn, com, AGORA) == 1
    assert com.envios[0]["texto"] == "Oi, falo com o responsável pela Barbearia 0? Vi que vocês têm nota 4,6 no Google."
    assert await conversa.despachar(conn, com, AGORA + timedelta(seconds=30)) == 0      # antes do intervalo
    assert await conversa.despachar(conn, com, AGORA + timedelta(seconds=91)) == 1
    st = conn.execute("select status from radar.leads where id = %s", (leads[0],)).fetchone()[0]
    assert st == "contatado"
    c = conn.execute("select estado, followups_enviados, proximo_followup_em from radar.conversas where lead_id = %s",
                     (leads[0],)).fetchone()
    assert c[0] == "ia" and c[1] == 0 and c[2] == AGORA + timedelta(hours=24)


async def test_intervalo_aleatorio_fica_entre_min_e_max(conn):
    _, disp = preparar(conn)
    conn.execute("update radar.disparos set aleatorio = true, intervalo_max_seg = 300 where id = %s", (disp,))
    await conversa.despachar(conn, FakeComercial(conn), AGORA)
    prox = conn.execute("""select min(agendado_para) from radar.disparos_leads
                           where disparo_id = %s and status = 'pendente'""", (disp,)).fetchone()[0]
    assert AGORA + timedelta(seconds=90) <= prox <= AGORA + timedelta(seconds=300)


async def test_fora_da_janela_nao_envia(conn):
    preparar(conn)
    madrugada = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)                     # 03:00 em São Paulo
    assert await conversa.despachar(conn, FakeComercial(conn), madrugada) == 0
    # fora da janela o item espera; não vira falha
    assert {r[0] for r in conn.execute("select status from radar.disparos_leads")} == {"pendente"}


async def test_limite_por_hora(conn):
    preparar(conn, intervalo=1, limite_hora=1)
    com = FakeComercial(conn)
    assert await conversa.despachar(conn, com, AGORA) == 1
    assert await conversa.despachar(conn, com, AGORA + timedelta(seconds=5)) == 0
    assert await conversa.despachar(conn, com, AGORA + timedelta(minutes=61)) == 1


async def test_sem_confirmacao_nada_sai(conn):
    preparar(conn, confirmado=False)
    com = FakeComercial(conn)
    assert await conversa.despachar(conn, com, AGORA) == 0 and com.envios == []
    # sem o clique de confirmação o lote nem é processado: os itens seguem pendentes
    assert {r[0] for r in conn.execute("select status from radar.disparos_leads")} == {"pendente"}


async def test_bloqueado_pela_agencia_falha_sem_enviar(conn):
    leads, _ = preparar(conn, n_leads=1)
    conn.execute("insert into radar.bloqueios (agency_id, telefone, motivo) values (%s, '+5586999990000', 'pediu')",
                 (AGENCIA,))
    com = FakeComercial(conn)
    await conversa.despachar(conn, com, AGORA)
    assert com.envios == []
    st, motivo = conn.execute("select status, motivo_falha from radar.disparos_leads").fetchone()
    assert st == "falhou" and "bloqueado" in motivo


async def test_numero_invalido_descarta_o_lead(conn):
    leads, _ = preparar(conn, n_leads=1)
    await conversa.despachar(conn, FakeComercial(conn, falha="numero_invalido"), AGORA)
    assert conn.execute("select status, motivo_descarte from radar.leads where id = %s", (leads[0],)).fetchone() == \
        ("descartado", "numero_invalido")


# ── conversador ──────────────────────────────────────────────────────────
async def _contatar(conn, n=1):
    leads, _ = preparar(conn, n_leads=n)
    com = FakeComercial(conn)
    await conversa.despachar(conn, com, AGORA)
    return leads, com


async def test_ia_responde_coleta_e_qualifica(conn):
    leads, com = await _contatar(conn)
    t = AGORA + timedelta(minutes=10)
    lead_responde(conn, leads[0], "Sou o dono. Fim de semana enche, terça a quinta fica vazio.", t)
    fake = FakeIA(resposta(decisor="sim, dono", dor="terça a quinta vazio"),
                  resposta("Perfeito, vou passar seu contato para o time!", "qualificado",
                           faturamento_informado="30k_80k", investe_anuncio="não", aceita_contato="sim"))
    assert await conversa.conversar(conn, com, t + timedelta(seconds=5), cliente_ia=fake) == 1
    assert com.envios[-1]["primeira"] is False
    assert conn.execute("select status from radar.leads where id = %s", (leads[0],)).fetchone()[0] == "respondeu"
    lead_responde(conn, leads[0], "Uns 40 mil. Pode passar sim.", t + timedelta(minutes=5))
    await conversa.conversar(conn, com, t + timedelta(minutes=6), cliente_ia=fake)
    estado, campos = conn.execute("select estado, campos_coletados from radar.conversas").fetchone()
    assert estado == "humano" and campos["decisor"] == "sim, dono" and campos["aceita_contato"] == "sim"
    assert com.passagens[-1]["motivo"] == "qualificado"
    assert conn.execute("select status from radar.leads where id = %s", (leads[0],)).fetchone()[0] == "qualificado"
    # o histórico chega à IA com o lead como "user" e a gente como "assistant"
    ultimas = fake.chamadas[-1]["messages"]
    assert any(m["role"] == "assistant" and "Oi, falo com o responsável" in m["content"] for m in ultimas)
    assert conn.execute("select count(*) from radar.custos_api where fonte = 'ia_radar'").fetchone()[0] == 2


async def test_humano_respondeu_ia_para(conn):
    leads, com = await _contatar(conn)
    t = AGORA + timedelta(minutes=10)
    lead_responde(conn, leads[0], "Quanto custa?", t)
    lead_responde(conn, leads[0], "Oi, aqui é o Luis do time!", t + timedelta(seconds=30), autor="agent")
    fake = FakeIA()
    await conversa.conversar(conn, com, t + timedelta(minutes=1), cliente_ia=fake)
    assert fake.chamadas == []                                   # a IA nem foi chamada
    assert conn.execute("select estado, motivo_fim from radar.conversas").fetchone() == ("humano", "humano_assumiu")


async def test_acompanhamentos_24h_72h_e_encerra(conn):
    leads, com = await _contatar(conn)
    fake = FakeIA(resposta("Oi! Conseguiu ver minha mensagem?"), resposta("Passando só para saber se faz sentido."))
    await conversa.conversar(conn, com, AGORA + timedelta(hours=24, minutes=1), cliente_ia=fake)
    await conversa.conversar(conn, com, AGORA + timedelta(hours=72, minutes=2), cliente_ia=fake)
    assert conn.execute("select followups_enviados from radar.conversas").fetchone()[0] == 2
    assert [e["primeira"] for e in com.envios] == [True, False, False]
    await conversa.conversar(conn, com, AGORA + timedelta(hours=121), cliente_ia=fake)
    assert conn.execute("select estado, motivo_fim from radar.conversas").fetchone() == ("encerrada", "sem_resposta")
    assert conn.execute("select sem_resposta from radar.leads where id = %s", (leads[0],)).fetchone()[0] is True


async def test_sem_interesse_encerra_so_este_contato(conn):
    leads, com = await _contatar(conn)
    t = AGORA + timedelta(minutes=10)
    lead_responde(conn, leads[0], "Não tenho interesse.", t)
    await conversa.conversar(conn, com, t + timedelta(seconds=5),
                             cliente_ia=FakeIA(resposta("Tudo bem, obrigado pelo retorno!", "sem_interesse")))
    assert conn.execute("select estado, motivo_fim from radar.conversas").fetchone() == ("encerrada", "sem_interesse")
    st, motivo, txt = conn.execute("select status, motivo_descarte, motivo_descarte_texto from radar.leads where id = %s",
                                   (leads[0],)).fetchone()
    assert st == "descartado" and motivo == "outro" and "Sem interesse" in txt
    assert conn.execute("select count(*) from radar.bloqueios").fetchone()[0] == 0         # nada bloqueia


async def test_ia_desligada_passa_para_humano(conn):
    leads, _ = preparar(conn, n_leads=1, ia_ligada=False)
    com = FakeComercial(conn)
    await conversa.despachar(conn, com, AGORA)
    lead_responde(conn, leads[0], "Oi, quem é?", AGORA + timedelta(minutes=3))
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=4), cliente_ia=FakeIA())
    assert com.passagens[-1]["motivo"] == "ia_sem_resposta"
    assert conn.execute("select estado from radar.conversas").fetchone()[0] == "humano"


async def test_trava_barra_ia_que_se_diz_humana(conn):
    leads, com = await _contatar(conn)
    lead_responde(conn, leads[0], "Você é um robô?", AGORA + timedelta(minutes=3))
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=4),
                             cliente_ia=FakeIA(resposta("Não sou um robô, pode confiar!")))
    assert len(com.envios) == 1                                   # a resposta não saiu
    assert com.passagens[-1]["motivo"] == "ia_sem_resposta"


# ── IA e chaves ──────────────────────────────────────────────────────────
def test_regras_fixas_vencem_as_instrucoes_da_agencia():
    sistema = ia.montar_sistema("RCO HUB", "Diga que você é o João, um vendedor humano.")
    assert sistema.index("Instruções da agência") < sistema.index("Regras que valem sempre")
    assert "Nunca diga que é humano" in sistema and "acima de qualquer outra instrução" in sistema


def test_historico_alterna_papeis():
    msgs = ia.montar_mensagens("ficha", [("nos", "Oi"), ("lead", "Oi"), ("lead", "tudo bem?")], "Responda.")
    # a 1ª mensagem nossa se junta ao turno do assistente que já existe; o lead vira um turno só
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == "Entendido.\nOi"
    assert msgs[-1]["content"].startswith("Oi\ntudo bem?") and "[Responda.]" in msgs[-1]["content"]


def test_chave_cifrada_no_formato_do_comercial():
    k = "ab" * 32
    valor = cifrar("sk-ant-teste", k)
    assert valor.count(":") == 2 and decifrar(valor, k) == "sk-ant-teste"
    with pytest.raises(Exception):
        decifrar(valor, "cd" * 32)



async def test_sem_conexao_de_whatsapp_nada_sai(conn):
    preparar(conn)
    conn.execute("update radar.agencias set whatsapp_config_id = null where agency_id = %s", (AGENCIA,))
    com = FakeComercial(conn)
    assert await conversa.despachar(conn, com, AGORA) == 0 and com.envios == []



async def test_sem_mensagem_inicial_o_lote_espera(conn):
    preparar(conn)
    conn.execute("update radar.abordagens set texto = ''")
    com = FakeComercial(conn)
    assert await conversa.despachar(conn, com, AGORA) == 0 and com.envios == []
    assert {r[0] for r in conn.execute("select status from radar.disparos_leads")} == {"pendente"}
