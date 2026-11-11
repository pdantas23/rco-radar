"""Triagem IA (docs/triagem-ia.md): conversa de LP, nota determinística, teto de custo e OpenRouter.

Banco: Postgres real (RADAR_TEST_DSN) com a 008. COMERCIAL, Anthropic e OpenRouter simulados: nada pago.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from radar import conversa, ia, triagem
from radar.chaves import cifrar
from radar.comercial import ResultadoEnvio
from tests.conftest import AGENCIA, CONTA_MESTRE
from tests.test_conversa import AGORA, CONEXAO, FakeIA, resposta

MADRUGADA = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)          # 03:00 em São Paulo
RESPOSTAS_LP = [{"pergunta": "Qual o seu segmento?", "resposta": "Clínica odontológica"},
                {"pergunta": "Quanto investe em anúncios por mês?", "resposta": "Uns 2 mil"}]

CRITERIOS = [  # o mesmo exemplo da migration 008
    {"campo": "decisor", "peso": 20, "regra": {"tipo": "em", "valores": ["sim"]}},
    {"campo": "faturamento_faixa", "peso": 25, "regra": {"tipo": "em", "valores": ["30k_80k", "80k_200k", "200k_500k", "acima_500k"]}},
    {"campo": "investe_anuncio", "peso": 15, "regra": {"tipo": "em", "valores": ["sim"]}},
    {"campo": "dor_clara", "peso": 20, "regra": {"tipo": "em", "valores": ["sim"]}},
    {"campo": "urgencia", "peso": 10, "regra": {"tipo": "em", "valores": ["alta", "media"]}},
    {"campo": "aceita_contato", "peso": 10, "regra": {"tipo": "em", "valores": ["sim"]}},
]


def crit(criterios=CRITERIOS, passar=70, descartar=30):
    return triagem.Criterios(1, 1, criterios, passar, descartar, True)


# ── nota e cortes (puro) ─────────────────────────────────────────────────
def test_tudo_atendido_da_100_e_passa():
    fatos = {"decisor": "sim", "faturamento_faixa": "80k_200k", "investe_anuncio": "sim", "dor_clara": "sim",
             "urgencia": "alta", "aceita_contato": "sim"}
    r = triagem.calcular(crit(), fatos, [])
    assert (r.nota, r.teto, r.decisao) == (100, 100, "passar")
    assert [i["pontos"] for i in r.detalhe["criterios"]] == [20, 25, 15, 20, 10, 10]


@pytest.mark.parametrize("nota,teto,esperado", [
    (70, 70, "passar"),          # exatamente no corte de passar
    (69, 100, "acompanhar"),     # um ponto abaixo
    (29, 30, "acompanhar"),      # teto exatamente no corte de descarte: ainda pode chegar lá
    (29, 29, "descartar"),       # teto abaixo do corte
    (0, 0, "descartar"),
    (30, 30, "acompanhar"),      # nota final no meio: segue com acompanhamentos
])
def test_bordas_dos_cortes(nota, teto, esperado):
    assert triagem.decidir(nota, teto, 70, 30) == esperado


def test_fato_desconhecido_nao_descarta():
    # só sabemos que NÃO é decisor e não investe: nota 0, mas o teto (65) ainda passa do corte de descarte
    r = triagem.calcular(crit(), {"decisor": "nao", "investe_anuncio": "nao", "urgencia": "indefinido"}, [])
    assert (r.nota, r.teto, r.decisao) == (0, 65, "acompanhar")
    assert r.detalhe["criterios"][4]["atende"] is None                  # urgência ainda desconhecida


def test_respostas_da_lp_contam_como_fato():
    c = crit([{"campo": "resposta:investe em anuncios", "peso": 60, "regra": {"tipo": "contem", "valores": ["mil"]}},
              {"campo": "resposta:segmento", "peso": 40, "regra": {"tipo": "preenchido"}}], passar=100, descartar=10)
    r = triagem.calcular(c, {}, RESPOSTAS_LP)
    assert (r.nota, r.decisao) == (100, "passar")                       # sem acento/maiúscula no trecho da pergunta


def test_pesos_fora_de_100_sao_normalizados_e_regra_desconhecida_falha():
    c = crit([{"campo": "decisor", "peso": 1, "regra": {"tipo": "em", "valores": ["sim"]}},
              {"campo": "dor_clara", "peso": 1, "regra": {"tipo": "em", "valores": ["sim"]}}])
    assert triagem.calcular(c, {"decisor": "sim", "dor_clara": "nao"}, []).nota == 50
    with pytest.raises(ValueError):
        triagem.calcular(crit([{"campo": "decisor", "peso": 100, "regra": {"tipo": "regex"}}]), {"decisor": "x"}, [])


def test_fato_conhecido_nao_volta_a_ser_desconhecido():
    assert triagem.juntar_fatos({"decisor": "sim", "urgencia": "baixa"},
                                {"decisor": "indefinido", "urgencia": "alta", "dor_clara": None}) == \
        {"decisor": "sim", "urgencia": "alta"}


# ── banco: conversa de LP ────────────────────────────────────────────────
class FakeComercialLP:
    """Imita /enviar (LP por conversa_id: cria a conversa do inbox e grava em radar.conversas), /passar e /descartar."""

    def __init__(self, conn, falha=None, erro_rede=False):
        self.conn, self.falha, self.erro_rede = conn, falha, erro_rede
        self.envios, self.passagens, self.descartes = [], [], []

    async def enviar(self, *, agency_id, lead_id, whatsapp_config_id, telefone, nome, texto, primeira,
                     idempotency_key=None, conversa_id=None):
        self.envios.append({"lead_id": lead_id, "conversa_id": conversa_id, "telefone": telefone, "texto": texto,
                            "primeira": primeira, "idempotency_key": idempotency_key})
        if self.erro_rede:
            raise RuntimeError("COMERCIAL /enviar: HTTP 502")
        if self.falha:
            return ResultadoEnvio(False, motivo=self.falha)
        conv_id, contato = self.conn.execute("select conversation_id, contact_id from radar.conversas where id = %s",
                                             (conversa_id,)).fetchone()
        if conv_id is None:
            conv_id = self.conn.execute("insert into public.conversations (contact_id) values (%s) returning id",
                                        (contato,)).fetchone()[0]
            self.conn.execute("update radar.conversas set conversation_id = %s where id = %s", (conv_id, conversa_id))
        self.conn.execute("""insert into public.messages (conversation_id, sender_type, content_text, created_at)
                             values (%s, 'bot', %s, %s)""", (conv_id, texto, AGORA))
        return ResultadoEnvio(True, str(conv_id), str(contato), "m1")

    async def passar(self, **kw):
        self.passagens.append(kw)
        return {"ok": True}

    async def descartar(self, **kw):
        self.descartes.append(kw)
        return {"ok": True, "etiquetado": True}


def preparar_lp(conn, n=1, criterios=True, limite_hora=20, intervalo=0, telefone=None, ia_ligada=True,
                provedor=None, limite_usd=None):
    conn.execute("""truncate radar.disparos_leads, radar.disparos, radar.abordagens, radar.conversas, radar.bloqueios,
                    radar.criterios_triagem, radar.custos_api, radar.config cascade""")
    conn.execute("truncate public.messages, public.conversations, public.agent_configs, public.agent_provider_keys cascade")
    conn.execute("""insert into public.agent_configs (agency_id, radar_agent_enabled, radar_agent_provider,
                    radar_agent_monthly_cost_limit_usd) values (%s, %s, %s, %s)""", (AGENCIA, ia_ligada, provedor, limite_usd))
    conn.execute("insert into public.whatsapp_config (id, account_id) values (%s, %s) on conflict do nothing",
                 (CONEXAO, CONTA_MESTRE))
    conn.execute("""update radar.agencias set whatsapp_config_id = %s, limite_hora = %s, intervalo_min_seg = %s,
                    intervalo_max_seg = %s where agency_id = %s""", (CONEXAO, limite_hora, intervalo, intervalo, AGENCIA))
    if criterios:
        conn.execute("""insert into radar.criterios_triagem (agency_id, origem, criterios, corte_passar, corte_descartar, exemplo)
                        values (%s, 'lp', %s::jsonb, 70, 30, true)""", (AGENCIA, json.dumps(CRITERIOS)))
    ids = []
    for i in range(n):
        contato = conn.execute("insert into public.contacts default values returning id").fetchone()[0]
        ctx = {"integracao_id": "i1", "respondent_id": f"r{i}", "lead_source": "lp-teste", "pipeline_id": None,
               "stage_id": None, "respostas": RESPOSTAS_LP, "utm": {"utm_source": "meta"}}
        if telefone:
            ctx["telefone"] = telefone
        ids.append(conn.execute(
            """insert into radar.conversas (agency_id, origem, estado, contact_id, whatsapp_config_id, contexto)
               values (%s, 'lp', 'ia', %s, %s, %s::jsonb) returning id""",
            (AGENCIA, contato, CONEXAO, json.dumps(ctx))).fetchone()[0])
    return ids


def conv(conn, cid):
    return conn.cursor(row_factory=__import__("psycopg").rows.dict_row).execute(
        "select * from radar.conversas where id = %s", (cid,)).fetchone()


def lead_lp_responde(conn, cid, texto, quando, autor="customer"):
    cv = conv(conn, cid)["conversation_id"]
    conn.execute("""insert into public.messages (conversation_id, sender_type, content_text, created_at)
                    values (%s, %s, %s, %s)""", (cv, autor, texto, quando))


async def test_lp_primeira_mensagem_automatica_dentro_da_janela(conn):
    [cid] = preparar_lp(conn)
    com = FakeComercialLP(conn)
    assert await conversa.despachar(conn, com, AGORA) == 1          # sem disparo, sem aprovação humana
    e = com.envios[0]
    assert e["lead_id"] is None and e["conversa_id"] == str(cid) and e["primeira"] is True
    assert e["idempotency_key"] == f"radar-lp-{cid}" and "RCO HUB" in e["texto"]
    c = conv(conn, cid)
    assert c["iniciada_em"] == AGORA and c["conversation_id"] is not None
    assert c["proximo_followup_em"] == AGORA + timedelta(hours=24) and c["contexto"]["primeira"]["status"] == "enviado"
    assert await conversa.despachar(conn, com, AGORA + timedelta(minutes=5)) == 0   # não repete


async def test_lp_fora_da_janela_espera(conn):
    [cid] = preparar_lp(conn)
    com = FakeComercialLP(conn)
    assert await conversa.despachar(conn, com, MADRUGADA) == 0 and com.envios == []
    c = conv(conn, cid)
    assert c["estado"] == "ia" and c["iniciada_em"] is None and "primeira" not in c["contexto"]
    assert await conversa.despachar(conn, com, AGORA) == 1          # a janela abriu


async def test_lp_trava_bloqueia_numero_bloqueado(conn):
    [cid] = preparar_lp(conn, telefone="+5586999990000")
    conn.execute("insert into radar.bloqueios (agency_id, telefone, motivo) values (%s, '+5586999990000', 'pediu')", (AGENCIA,))
    com = FakeComercialLP(conn)
    assert await conversa.despachar(conn, com, AGORA) == 0 and com.envios == []
    c = conv(conn, cid)
    assert c["contexto"]["primeira"]["status"] == "falhou" and "bloqueado" in c["contexto"]["primeira"]["motivo"]
    assert (c["estado"], c["motivo_fim"]) == ("humano", "falha_primeira_mensagem")


async def test_lp_trava_bloqueia_texto_com_variavel_nao_preenchida(conn):
    [cid] = preparar_lp(conn)
    conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, 'triagem_mensagens', %s::jsonb)""",
                 (AGENCIA, json.dumps({"lp": {"inicial": "Oi {nome}, aqui é da {agencia}!"}})))
    com = FakeComercialLP(conn)
    assert await conversa.despachar(conn, com, AGORA) == 0 and com.envios == []
    assert "variável" in conv(conn, cid)["contexto"]["primeira"]["motivo"]


async def test_lp_mensagem_inicial_vem_da_config_da_agencia(conn):
    preparar_lp(conn)
    conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, 'triagem_mensagens', %s::jsonb)""",
                 (AGENCIA, json.dumps({"lp": {"inicial": "Oi! Aqui é da {agencia}, vi seu cadastro."}})))
    com = FakeComercialLP(conn)
    await conversa.despachar(conn, com, AGORA)
    assert com.envios[0]["texto"] == "Oi! Aqui é da RCO HUB, vi seu cadastro."


async def test_lp_limite_por_hora_e_intervalo_da_agencia(conn):
    preparar_lp(conn, n=3, limite_hora=2, intervalo=60)
    com = FakeComercialLP(conn)
    assert await conversa.despachar(conn, com, AGORA) == 1
    assert await conversa.despachar(conn, com, AGORA + timedelta(seconds=30)) == 0  # intervalo mínimo
    assert await conversa.despachar(conn, com, AGORA + timedelta(seconds=61)) == 1
    assert await conversa.despachar(conn, com, AGORA + timedelta(seconds=200)) == 0  # limite de 2 por hora
    assert await conversa.despachar(conn, com, AGORA + timedelta(minutes=61)) == 1


async def test_lp_radar_desligado_nada_sai(conn):
    preparar_lp(conn)
    conn.execute("update radar.agencias set ativo = false where agency_id = %s", (AGENCIA,))
    com = FakeComercialLP(conn)
    assert await conversa.despachar(conn, com, AGORA) == 0 and com.envios == []


async def test_lp_erro_de_rede_volta_para_a_fila(conn):
    [cid] = preparar_lp(conn)
    com = FakeComercialLP(conn, erro_rede=True)
    assert await conversa.despachar(conn, com, AGORA) == 0
    p = conv(conn, cid)["contexto"]["primeira"]
    assert p["status"] == "pendente" and "502" in p["motivo"]
    com.erro_rede = False
    assert await conversa.despachar(conn, com, AGORA + timedelta(minutes=1)) == 0     # espera 5 min
    assert await conversa.despachar(conn, com, AGORA + timedelta(minutes=6)) == 1


async def test_lp_numero_invalido_registra_falha(conn):
    [cid] = preparar_lp(conn)
    await conversa.despachar(conn, FakeComercialLP(conn, falha="numero_invalido"), AGORA)
    c = conv(conn, cid)
    assert c["contexto"]["primeira"]["status"] == "falhou" and c["contexto"]["primeira"]["motivo"] == "numero_invalido"
    assert c["iniciada_em"] is None and c["estado"] == "humano"


# ── banco: conversa de LP com nota ───────────────────────────────────────
async def _lp_contatada(conn, **kw):
    [cid] = preparar_lp(conn, **kw)
    com = FakeComercialLP(conn)
    await conversa.despachar(conn, com, AGORA)
    return cid, com


def fatos(**kw):
    return ia.Fatos(**kw)


def resp_fatos(mensagem="E hoje vocês investem em anúncio?", intencao="continuar", **f):
    r = resposta(mensagem, intencao)
    r.fatos = fatos(**f)
    return r


async def test_lp_ia_recebe_as_respostas_da_lp_e_prompt_de_lp(conn):
    cid, com = await _lp_contatada(conn)
    lead_lp_responde(conn, cid, "Oi, pode falar", AGORA + timedelta(minutes=2))
    fake = FakeIA(resp_fatos())
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3), cliente_ia=fake)
    chamada = fake.chamadas[0]
    assert "landing page" in chamada["system"] and "NÃO pergunte de novo" in chamada["system"]
    primeira = chamada["messages"][0]["content"]
    assert "Clínica odontológica" in primeira and "Quanto investe em anúncios por mês?: Uns 2 mil" in primeira


async def test_lp_nota_recalculada_a_cada_resposta_e_passa(conn):
    cid, com = await _lp_contatada(conn)
    t = AGORA + timedelta(minutes=2)
    lead_lp_responde(conn, cid, "Sou a dona, faturo uns 100 mil", t)
    fake = FakeIA(resp_fatos(decisor="sim", faturamento_faixa="80k_200k"),
                  resp_fatos("Perfeito!", "continuar", investe_anuncio="sim", dor_clara="sim"))
    await conversa.conversar(conn, com, t + timedelta(seconds=5), cliente_ia=fake)
    c = conv(conn, cid)
    assert (c["nota"], c["decisao"], c["estado"], c["versao_criterios"]) == (45, "acompanhar", "ia", 1)
    assert c["nota_detalhe"]["fatos"] == {"decisor": "sim", "faturamento_faixa": "80k_200k"}
    assert com.envios[-1]["texto"] == "E hoje vocês investem em anúncio?" and com.envios[-1]["conversa_id"] == str(cid)

    lead_lp_responde(conn, cid, "Invisto sim, quero mais pacientes", t + timedelta(minutes=5))
    await conversa.conversar(conn, com, t + timedelta(minutes=6), cliente_ia=fake)
    c = conv(conn, cid)
    assert (c["nota"], c["decisao"], c["estado"], c["motivo_fim"]) == (80, "passar", "humano", "qualificado")
    p = com.passagens[-1]
    assert p["lead_id"] is None and p["conversation_id"] == c["conversation_id"] and p["motivo"] == "qualificado"
    assert p["campos"]["decisor"] == "sim" and "80/100" in p["resumo"]
    assert com.envios[-1]["texto"] == triagem.MENSAGENS_PADRAO["lp"]["passar"]   # texto de passagem da config


async def test_lp_descarta_pela_nota(conn):
    cid, com = await _lp_contatada(conn)
    lead_lp_responde(conn, cid, "Sou funcionário, a loja fatura 10 mil, não anuncia, sem pressa", AGORA + timedelta(minutes=2))
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3), cliente_ia=FakeIA(resp_fatos(
        decisor="nao", faturamento_faixa="ate_30k", investe_anuncio="nao", urgencia="baixa", dor_clara="nao")))
    c = conv(conn, cid)                                   # nota 0, teto 10 (< 30)
    assert (c["nota"], c["decisao"], c["estado"], c["motivo_fim"]) == (0, "descartar", "encerrada", "fora_do_perfil")
    assert [{**d, "agency_id": str(d["agency_id"])} for d in com.descartes] == \
        [{"agency_id": AGENCIA, "conversa_id": str(cid), "motivo": "Fora do perfil (nota 0/100, corte 30)"}]
    assert com.passagens == [] and com.envios[-1]["texto"] == triagem.MENSAGENS_PADRAO["lp"]["descartar"]


async def test_ia_dizer_qualificado_nao_passa_sem_nota(conn):
    cid, com = await _lp_contatada(conn)
    lead_lp_responde(conn, cid, "Sou o dono", AGORA + timedelta(minutes=2))
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3),
                             cliente_ia=FakeIA(resp_fatos("Ótimo!", "qualificado", decisor="sim")))
    c = conv(conn, cid)
    assert (c["nota"], c["decisao"], c["estado"]) == (20, "acompanhar", "ia") and com.passagens == []


async def test_lp_humano_assume(conn):
    cid, com = await _lp_contatada(conn)
    lead_lp_responde(conn, cid, "Quanto custa?", AGORA + timedelta(minutes=2))
    lead_lp_responde(conn, cid, "Oi, aqui é o Luis!", AGORA + timedelta(minutes=3), autor="agent")
    fake = FakeIA()
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=4), cliente_ia=fake)
    assert fake.chamadas == []
    assert (conv(conn, cid)["estado"], conv(conn, cid)["motivo_fim"]) == ("humano", "humano_assumiu")


async def test_lp_pediu_humano_passa_mesmo_com_nota_baixa(conn):
    cid, com = await _lp_contatada(conn)
    lead_lp_responde(conn, cid, "Quero falar com uma pessoa", AGORA + timedelta(minutes=2))
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3),
                             cliente_ia=FakeIA(resp_fatos("Claro, já chamo alguém!", "pediu_humano")))
    assert com.passagens[-1]["motivo"] == "pediu_humano" and conv(conn, cid)["estado"] == "humano"


async def test_radar_com_criterios_descarta_lead_sem_rota_de_lp(conn):
    from tests.test_conversa import _contatar, lead_responde
    leads, com = await _contatar(conn)
    conn.execute("""insert into radar.criterios_triagem (agency_id, origem, criterios, corte_passar, corte_descartar)
                    values (%s, 'radar', %s::jsonb, 70, 30)""", (AGENCIA, json.dumps(CRITERIOS)))
    lead_responde(conn, leads[0], "Não sou o dono e não anunciamos", AGORA + timedelta(minutes=2))
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3), cliente_ia=FakeIA(resp_fatos(
        decisor="nao", faturamento_faixa="ate_30k", investe_anuncio="nao", urgencia="baixa", dor_clara="nao")))
    assert conn.execute("select estado, motivo_fim, decisao from radar.conversas").fetchone() == \
        ("encerrada", "fora_do_perfil", "descartar")
    st, txt = conn.execute("select status, motivo_descarte_texto from radar.leads where id = %s", (leads[0],)).fetchone()
    assert st == "descartado" and "Fora do perfil" in txt


# ── teto de custo ────────────────────────────────────────────────────────
@pytest.mark.parametrize("gasto_usd,chama", [(0.99, True), (1.0, False), (3.0, False)])
async def test_teto_de_custo(conn, gasto_usd, chama):
    cid, com = await _lp_contatada(conn, limite_usd=1.0)
    conn.execute("""insert into radar.custos_api (agency_id, fonte, operacao, qtd, valor, estimado, em)
                    values (%s, 'ia_radar', 'conversa', 1, %s, false, %s)""", (AGENCIA, gasto_usd * ia.DOLAR, AGORA))
    # gasto do mês passado não conta
    conn.execute("""insert into radar.custos_api (agency_id, fonte, operacao, qtd, valor, estimado, em)
                    values (%s, 'ia_radar', 'conversa', 1, 999, false, %s)""", (AGENCIA, AGORA - timedelta(days=40)))
    lead_lp_responde(conn, cid, "Oi", AGORA + timedelta(minutes=2))
    fake = FakeIA(resp_fatos())
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3), cliente_ia=fake)
    assert bool(fake.chamadas) is chama
    c = conv(conn, cid)
    if chama:
        assert c["estado"] == "ia"
    else:
        assert (c["estado"], c["motivo_fim"]) == ("humano", "limite_custo")
        assert com.passagens[-1]["motivo"] == "ia_sem_resposta" and "limite de custo" in com.passagens[-1]["resumo"]


# ── OpenRouter (falso) ───────────────────────────────────────────────────
def _openrouter_fake(status=200, custo=0.0021, corpo=None):
    chamadas = []

    def handler(req: httpx.Request):
        chamadas.append(req)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "sem créditos"}})
        conteudo = corpo or resp_fatos("Legal! Vocês investem em anúncio?", decisor="sim").model_dump_json()
        uso = {"prompt_tokens": 1000, "completion_tokens": 100}
        if custo is not None:
            uso["cost"] = custo
        return httpx.Response(200, json={"choices": [{"message": {"content": conteudo}}], "usage": uso})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), chamadas


async def test_openrouter_escolhido_pela_config_do_agente(conn, monkeypatch):
    chave = "ab" * 32
    monkeypatch.setenv("ENCRYPTION_KEY", chave)
    cid, com = await _lp_contatada(conn, provedor="openrouter")
    conn.execute("insert into public.agent_provider_keys (agency_id, provider, api_key_encrypted) values (%s, 'openrouter', %s)",
                 (AGENCIA, cifrar("sk-or-teste", chave)))
    conn.execute("update public.agent_configs set radar_agent_model = 'openai/gpt-5.6-luna' where agency_id = %s", (AGENCIA,))
    lead_lp_responde(conn, cid, "Sou o dono", AGORA + timedelta(minutes=2))
    http, chamadas = _openrouter_fake()
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3), cliente_ia=http)
    req = chamadas[0]
    corpo = json.loads(req.content)
    assert str(req.url) == ia.OPENROUTER_URL and req.headers["authorization"] == "Bearer sk-or-teste"
    assert corpo["model"] == "openai/gpt-5.6-luna" and corpo["messages"][0]["role"] == "system"
    schema = corpo["response_format"]["json_schema"]
    assert schema["strict"] is True and schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == {"mensagem", "campos", "fatos", "intencao", "resumo"}
    assert com.envios[-1]["texto"] == "Legal! Vocês investem em anúncio?" and conv(conn, cid)["nota"] == 20
    valor = conn.execute("select valor from radar.custos_api where fonte = 'ia_radar'").fetchone()[0]
    assert float(valor) == pytest.approx(0.0021 * ia.DOLAR)             # custo informado pela OpenRouter


async def test_openrouter_sem_custo_informado_usa_a_tabela():
    cfg = ia.ConfigIA(True, "openrouter", "openai/gpt-5.6-luna", "k", None, None)
    http, _ = _openrouter_fake(custo=None)
    _, custo = await ia.responder(cfg, "RCO", "ficha", [("lead", "oi")], cliente=http)
    assert custo == ia.custo_brl("openai/gpt-5.6-luna", 1000, 100)


@pytest.mark.parametrize("status,erro", [(402, ia.IAIndisponivel), (401, ia.IAIndisponivel), (503, RuntimeError)])
async def test_openrouter_erros(status, erro):
    cfg = ia.ConfigIA(True, "openrouter", None, "k", None, None)
    http, _ = _openrouter_fake(status=status)
    with pytest.raises(erro):
        await ia.responder(cfg, "RCO", "ficha", [("lead", "oi")], cliente=http)


async def test_openrouter_json_invalido_vira_ia_indisponivel():
    cfg = ia.ConfigIA(True, "openrouter", None, "k", None, None)
    http, _ = _openrouter_fake(corpo="não é json")
    with pytest.raises(ia.IAIndisponivel):
        await ia.responder(cfg, "RCO", "ficha", [("lead", "oi")], cliente=http)


async def test_provedor_nao_suportado_continua_recusado():
    with pytest.raises(ia.IAIndisponivel):
        await ia.responder(ia.ConfigIA(True, "gemini", None, "k", None, None), "RCO", "f", [("lead", "oi")])


# ── cliente das rotas do COMERCIAL ───────────────────────────────────────
async def test_comercial_serializa_uuid_e_manda_os_corpos_de_lp():
    import uuid

    from radar.comercial import Comercial
    recebidos = []

    def handler(req: httpx.Request):
        recebidos.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "conversation_id": "c1", "contact_id": "k1"})

    com = Comercial("http://comercial", "s", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ag, cv = uuid.uuid4(), uuid.uuid4()
    r = await com.enviar(agency_id=ag, lead_id=None, conversa_id=cv, whatsapp_config_id=uuid.uuid4(), telefone=None,
                         nome="", texto="Oi", primeira=True, idempotency_key="radar-lp-x")
    await com.descartar(agency_id=ag, conversa_id=cv, motivo="Fora do perfil")
    assert r.ok and recebidos[0][0] == "/api/internal/radar/enviar"
    assert recebidos[0][1]["lead_id"] is None and recebidos[0][1]["conversa_id"] == str(cv)
    assert recebidos[1] == ("/api/internal/radar/descartar",
                            {"agency_id": str(ag), "conversa_id": str(cv), "motivo": "Fora do perfil"})
