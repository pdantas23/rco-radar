"""Triagem IA (docs/triagem-ia.md): conversa de LP (1ª mensagem automática), teto de custo e OpenRouter.

Portões, cadência, esteira e agenda da v2: tests/test_triagem_v2.py.

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

# ── banco: conversa de LP ────────────────────────────────────────────────
class FakeComercialLP:
    """Imita /enviar (LP por conversa_id: cria a conversa do inbox e grava em radar.conversas), /passar e /descartar."""

    def __init__(self, conn, falha=None, erro_rede=False, passar_cai=False):
        self.conn, self.falha, self.erro_rede, self.passar_cai = conn, falha, erro_rede, passar_cai
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
        if self.passar_cai:
            raise RuntimeError("COMERCIAL /passar: HTTP 502")
        return {"ok": True}

    async def descartar(self, **kw):
        self.descartes.append(kw)
        return {"ok": True, "etiquetado": True}


def preparar_lp(conn, n=1, limite_hora=20, intervalo=0, telefone=None, ia_ligada=True,
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
    # v2: toque 2 da cadência em +2 h (terça 12h em SP, dentro da janela seg-sex 9h-18h)
    assert c["proximo_followup_em"] == AGORA + timedelta(hours=2) and c["cadencia_inicio"] == AGORA
    assert c["contexto"]["primeira"]["status"] == "enviado"
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
    p = com.passagens[-1]                                 # o lead vai ao funil como hoje
    assert p["motivo"] == "falha_primeira_mensagem" and p["conversa_id"] == str(cid) and "bloqueado" in p["resumo"]


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
    com = FakeComercialLP(conn, falha="numero_invalido")
    await conversa.despachar(conn, com, AGORA)
    c = conv(conn, cid)
    assert c["contexto"]["primeira"]["status"] == "falhou" and c["contexto"]["primeira"]["motivo"] == "numero_invalido"
    assert c["iniciada_em"] is None and (c["estado"], c["motivo_fim"]) == ("humano", "falha_primeira_mensagem")
    assert com.passagens == [{"agency_id": com.passagens[0]["agency_id"], "lead_id": None, "conversation_id": None,
                              "conversa_id": str(cid), "motivo": "falha_primeira_mensagem", "campos": {},
                              "resumo": "numero_invalido"}]


async def test_lp_falha_e_passar_fora_do_ar_tenta_de_novo_sem_perder_o_lead(conn):
    [cid] = preparar_lp(conn)
    com = FakeComercialLP(conn, falha="numero_invalido", passar_cai=True)
    await conversa.despachar(conn, com, AGORA)
    c = conv(conn, cid)
    assert c["estado"] == "ia" and c["contexto"]["primeira"]["status"] == "falhou"   # nada perdido
    await conversa.despachar(conn, com, AGORA + timedelta(minutes=2))                # antes dos 5 min
    assert len(com.passagens) == 1 and len(com.envios) == 1                          # nem reenvia a mensagem
    com.passar_cai = False
    await conversa.despachar(conn, com, AGORA + timedelta(minutes=6))
    c = conv(conn, cid)
    assert len(com.passagens) == 2 and len(com.envios) == 1
    assert (c["estado"], c["motivo_fim"]) == ("humano", "falha_primeira_mensagem")
    await conversa.despachar(conn, com, AGORA + timedelta(minutes=20))
    assert len(com.passagens) == 2                                                   # não repassa de novo


async def test_lp_telefone_null_nao_quebra_o_despacho(conn):
    [cid] = preparar_lp(conn)
    conn.execute("""update radar.conversas set contexto = contexto || '{"telefone": null, "nome": "Ana"}'::jsonb
                    where id = %s""", (cid,))
    conn.execute("insert into radar.bloqueios (agency_id, telefone, motivo) values (%s, '+5586999990000', 'pediu')", (AGENCIA,))
    com = FakeComercialLP(conn)
    assert await conversa.despachar(conn, com, AGORA) == 1
    assert com.envios[0]["telefone"] is None and conv(conn, cid)["contexto"]["primeira"]["status"] == "enviado"


async def test_lp_bloqueio_usa_o_telefone_do_contexto(conn):
    [bloq, livre] = preparar_lp(conn, n=2)
    for cid, tel in ((bloq, "+5586999990000"), (livre, "+5586988887777")):
        conn.execute("""update radar.conversas set contexto = contexto || jsonb_build_object('telefone', %s::text, 'nome', 'X')
                        where id = %s""", (tel, cid))
    conn.execute("insert into radar.bloqueios (agency_id, telefone, motivo) values (%s, '+5586999990000', 'pediu')", (AGENCIA,))
    com = FakeComercialLP(conn)
    await conversa.despachar(conn, com, AGORA)
    assert [e["conversa_id"] for e in com.envios] == [str(livre)] and com.envios[0]["telefone"] == "+5586988887777"
    assert conv(conn, bloq)["contexto"]["primeira"]["status"] == "falhou"


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
    assert "landing page" in chamada["system"] and "NÃO se pergunta de novo" in chamada["system"]
    primeira = chamada["messages"][0]["content"]
    assert "Clínica odontológica" in primeira and "Quanto investe em anúncios por mês?: Uns 2 mil" in primeira


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
        conteudo = corpo or resp_fatos("Legal! Vocês investem em anúncio?", decisor="dono_socio").model_dump_json()
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
    assert set(schema["schema"]["required"]) == {"mensagem", "campos", "fatos", "intencao", "resumo", "escolha_horario"}
    c = conv(conn, cid)
    assert com.envios[-1]["texto"] == "Legal! Vocês investem em anúncio?" and c["decisao"] == "em_andamento"
    assert c["nota_detalhe"]["fatos"]["decisor"] == "dono_socio"
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


# ── config gravada pela tela do COMERCIAL (radar.config / criterios_triagem) ──
def _config(conn, chave, valor):
    conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, %s, %s::jsonb)
                    on conflict (agency_id, chave) do update set valor = excluded.valor""", (AGENCIA, chave, json.dumps(valor)))


@pytest.mark.parametrize("valor,esperado", [
    (None, (2, (24, 48))),                                                      # chave ausente: padrão
    ({"quantidade": 3, "intervalos_horas": [12, 24, 36]}, (3, (12, 24, 36))),
    ({"quantidade": 0, "intervalos_horas": []}, (0, (24,))),
    ({"quantidade": 1, "intervalos_horas": [6, 6, 6]}, (1, (6, 6, 6))),
    ({"quantidade": 6, "intervalos_horas": [1] * 6}, (2, (24, 48))),            # acima de 5
    ({"quantidade": -1, "intervalos_horas": [1]}, (2, (24, 48))),
    ({"quantidade": 2, "intervalos_horas": [24]}, (2, (24, 48))),               # menos intervalos que acompanhamentos
    ({"quantidade": 2, "intervalos_horas": [24, 0]}, (2, (24, 48))),            # intervalo não positivo
    ({"quantidade": 2, "intervalos_horas": [24, "48"]}, (2, (24, 48))),
    ({"quantidade": True, "intervalos_horas": [24]}, (2, (24, 48))),
    ({"quantidade": 1.5, "intervalos_horas": [24, 48]}, (2, (24, 48))),
    ("lixo", (2, (24, 48))),
])
def test_followups_da_config_e_validacao(conn, valor, esperado):
    conn.execute("truncate radar.config")
    if valor is not None:
        _config(conn, "triagem_followups", valor)
    q, intervalos = conversa.followups(conn, AGENCIA)
    assert (q, tuple(i.total_seconds() / 3600 for i in intervalos)) == esperado
