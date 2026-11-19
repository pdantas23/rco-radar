"""Triagem IA v2 (docs/triagem-ia.md, seção v2): portões, 4 saídas, cadência de 5 toques, esteira e agenda.

Puros: radar/triagem.py. Banco: Postgres real (RADAR_TEST_DSN) com a 009. COMERCIAL por stub HTTP (cliente
radar.comercial.Comercial de verdade sobre httpx.MockTransport) e IA falsa: nada pago.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from radar import conversa, ia, triagem
from radar.comercial import Comercial
from tests.conftest import AGENCIA
from tests.test_conversa import AGORA, FakeIA
from tests.test_triagem import conv, lead_lp_responde, preparar_lp

R = triagem.REGRAS_PADRAO
SP = "America/Sao_Paulo"
JANELA = triagem.CADENCIA_PADRAO["janela"]
TODOS_OK = dict(segmento="Clínica odontológica", cidade="Teresina", investe_anuncio="sim", necessidade_clara="sim",
                dor="agenda vazia de terça a quinta", experiencia_agencia="nunca teve", decisor="dono_socio",
                prazo_inicio="ate_30d", faixa_faturamento="60_100k")
SDR = "00000000-0000-0000-0000-00000000d001"
OPCOES = [{"inicio": "2026-10-21T10:00:00-03:00", "fim": "2026-10-21T10:30:00-03:00", "rotulo": "quarta (21/10) às 10h"},
          {"inicio": "2026-10-22T15:00:00-03:00", "fim": "2026-10-22T15:30:00-03:00", "rotulo": "quinta (22/10) às 15h"}]
OPCOES_NOVAS = [{"inicio": "2026-10-23T09:00:00-03:00", "fim": "2026-10-23T09:30:00-03:00", "rotulo": "sexta (23/10) às 9h"},
                {"inicio": "2026-10-26T14:00:00-03:00", "fim": "2026-10-26T14:30:00-03:00", "rotulo": "segunda (26/10) às 14h"}]


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def av(**extra):
    return triagem.avaliar({**TODOS_OK, **extra}, R)


# ── saídas (puro) ────────────────────────────────────────────────────────
def test_passa_direto_com_todos_os_portoes_ok():
    r = av()
    assert r.classificacao == "passa_direto" and r.alertas == []
    assert "todos os portões ok" in r.justificativa and "Teresina" in r.justificativa


@pytest.mark.parametrize("extra,trecho", [
    ({"cidade": "Timon"}, "avaliação comercial"),
    ({"cidade": "São Luís"}, "ticket diferenciado"),
    ({"decisor": "participa"}, "participa da decisão"),
    ({"decisor": "nao_decide", "contato_decisor": "Carlos 86 9999-0000"}, "contato do decisor: Carlos"),
    ({"faixa_faturamento": "30_60k", "faixa_refino": "perto_do_maior"}, "entre R$ 50 mil e R$ 60 mil"),
    ({"faturamento_valor": 55000}, "entre R$ 50 mil e R$ 60 mil"),
])
def test_passa_alerta_diz_em_que_criterio(extra, trecho):
    r = av(**extra)
    assert r.classificacao == "passa_alerta" and len(r.alertas) == 1
    assert trecho in r.alertas[0] and trecho in r.justificativa


def test_dois_alertas_tambem_passam_com_os_dois_no_resumo():
    r = av(cidade="Timon", decisor="participa")
    assert r.classificacao == "passa_alerta" and len(r.alertas) == 2


@pytest.mark.parametrize("prazo", ["mais_60d", "so_pesquisando"])
def test_esteira_por_timing(prazo):
    r = av(prazo_inicio=prazo)
    assert r.classificacao == "esteira" and prazo in r.justificativa


@pytest.mark.parametrize("extra,trecho", [
    ({"ve_marketing_como_despesa": "sim"}, "vê marketing como despesa"),
    ({"orcamento_ou_intencao": "nao"}, "sem orçamento nem intenção"),
    ({"pede_algo_fora": "sim"}, "pede algo fora"),
])
def test_eliminatorio_desqualifica_na_hora_mesmo_com_fatos_faltando(extra, trecho):
    r = triagem.avaliar({"segmento": "loja", **extra}, R)            # quase tudo desconhecido
    assert r.classificacao == "desqualifica" and trecho in r.justificativa


def test_necessidade_nao_desqualifica_no_fim():
    assert triagem.avaliar({"necessidade_clara": "nao"}, R).classificacao == "em_andamento"
    r = av(necessidade_clara="nao")
    assert r.classificacao == "desqualifica" and "sem necessidade clara" in r.justificativa


@pytest.mark.parametrize("extra", [{"faixa_faturamento": "ate_30k"}, {"faturamento_valor": 40000},
                                   {"faixa_faturamento": "30_60k", "faixa_refino": "perto_do_menor"}])
def test_falha_so_por_porte_redireciona_para_trafego_isolado(extra):
    r = av(**extra)
    assert r.classificacao == "redireciona" and r.alertas[0].startswith("tráfego isolado")


def test_porte_e_outra_falha_desqualifica():
    assert av(faixa_faturamento="ate_30k", necessidade_clara="nao").classificacao == "desqualifica"


def test_porte_e_timing_longo_redireciona():
    assert av(faixa_faturamento="ate_30k", prazo_inicio="mais_60d").classificacao == "redireciona"


def test_porte_minimo_do_trafego_isolado_quando_definido():
    r2 = {**R, "trafego_isolado_porte_min": 35000}
    assert triagem.avaliar({**TODOS_OK, "faixa_faturamento": "ate_30k"}, r2).classificacao == "desqualifica"
    assert triagem.avaliar({**TODOS_OK, "faturamento_valor": 40000}, r2).classificacao == "redireciona"


def test_em_andamento_enquanto_falta_fato():
    r = av(prazo_inicio=None)
    assert r.classificacao == "em_andamento" and r.falta == ["timing"]


def test_decisor_indefinido_so_segura_enquanto_falta_outro_fato_e_no_fim_vira_alerta():
    assert av(decisor=None, prazo_inicio=None).classificacao == "em_andamento"
    r = av(decisor=None)
    assert r.classificacao == "passa_alerta" and "no fim da conversa" in r.alertas[0]
    r = av(decisor="nao_decide")                                       # não descarta
    assert r.classificacao == "passa_alerta" and "contato do decisor" in r.alertas[0]


def test_faixa_30_60_sem_refino_espera_o_refino():
    r = av(faixa_faturamento="30_60k")
    assert r.classificacao == "em_andamento" and r.falta == ["faturamento"]
    assert triagem.proxima_etapa({**TODOS_OK, "faixa_faturamento": "30_60k"}, R) == ("refino", False)


def test_mesma_entrada_mesmo_resultado_e_nao_altera_a_entrada():
    fatos = {**TODOS_OK, "decisor": None, "cidade": "Timon"}
    copia = dict(fatos)
    a, b = triagem.avaliar(fatos, R), triagem.avaliar(fatos, R)
    assert a.detalhe(fatos) == b.detalhe(fatos) and fatos == copia


# ── cada portão (puro) ───────────────────────────────────────────────────
@pytest.mark.parametrize("fatos,status", [
    ({"faturamento_valor": 60000}, "ok"), ({"faturamento_valor": 59999}, "alerta"),
    ({"faturamento_valor": 50000}, "alerta"), ({"faturamento_valor": 49999}, "falha"),
    ({"faixa_faturamento": "acima_100k"}, "ok"), ({"faixa_faturamento": "60_100k"}, "ok"),
    ({"faixa_faturamento": "ate_30k"}, "falha"), ({"faixa_faturamento": "30_60k"}, "pendente"),
    ({"faixa_faturamento": "30_60k", "faixa_refino": "perto_do_maior"}, "alerta"),
    ({"faixa_faturamento": "30_60k", "faixa_refino": "perto_do_menor"}, "falha"),
    ({"faixa_faturamento": "60_100k", "faturamento_valor": 45000}, "falha"),   # número dito vale mais que a faixa
    ({}, "pendente"),
])
def test_portao_faturamento(fatos, status):
    assert triagem.portao_faturamento(fatos, R).status == status


@pytest.mark.parametrize("d,contato,status", [("dono_socio", None, "ok"), ("participa", None, "alerta"),
                                              ("nao_decide", None, "pendente"), ("nao_decide", "Ana", "alerta"),
                                              (None, None, "pendente")])
def test_portao_decisor(d, contato, status):
    assert triagem.portao_decisor({"decisor": d, "contato_decisor": contato}).status == status


@pytest.mark.parametrize("cidade,status,trecho", [
    ("Teresina", "ok", "Opala"), ("teresina - PI", "ok", "Opala"), ("TERESINA", "ok", "Opala"),
    ("Timon", "alerta", "avaliação comercial"), ("Timon-MA", "alerta", "avaliação comercial"),
    ("Fortaleza", "alerta", "ticket diferenciado"), ("Teresópolis", "alerta", "ticket diferenciado"),
    (None, "pendente", "ainda não"),
])
def test_portao_cidade(cidade, status, trecho):
    p = triagem.portao_cidade({"cidade": cidade}, R)
    assert p.status == status and trecho in p.motivo


@pytest.mark.parametrize("v,status", [("sim", "ok"), ("nao", "falha"), ("indefinido", "pendente")])
def test_portao_necessidade(v, status):
    assert triagem.portao_necessidade({"necessidade_clara": v}).status == status


@pytest.mark.parametrize("v,status", [("ate_30d", "ok"), ("30_60d", "ok"), ("mais_60d", "esteira"),
                                      ("so_pesquisando", "esteira"), ("indefinido", "pendente")])
def test_portao_timing(v, status):
    assert triagem.portao_timing({"prazo_inicio": v}, R).status == status


def test_portao_telefone_conversa_ativa():
    assert triagem.avaliar(TODOS_OK, R, conversa_ativa=False).classificacao == "desqualifica"
    tel = next(p for p in triagem.portoes(TODOS_OK, R) if p.portao == "telefone")
    assert tel.status == "ok" and "WhatsApp" in tel.motivo


def test_regras_vem_da_config_da_agencia_e_valor_invalido_cai_no_padrao(conn):
    conn.execute("truncate radar.config")
    assert triagem.regras(conn, AGENCIA) == R
    conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, 'triagem_regras', %s::jsonb)""",
                 (AGENCIA, json.dumps({"faturamento": {"passa_min": 80000, "alerta_min": "x"},
                                       "cidades": {"proximas": ["Timon", "José de Freitas"]},
                                       "trafego_isolado_porte_min": "lixo", "desconhecida": 1})))
    r = triagem.regras(conn, AGENCIA)
    assert r["faturamento"] == {"passa_min": 80000, "alerta_min": 50000}
    assert r["cidades"] == {"opala": ["Teresina"], "proximas": ["Timon", "José de Freitas"]}
    assert r["trafego_isolado_porte_min"] is None and "desconhecida" not in r
    assert triagem.avaliar({**TODOS_OK, "faturamento_valor": 70000}, r).classificacao == "passa_alerta"
    assert triagem.avaliar({**TODOS_OK, "cidade": "José de Freitas", "faturamento_valor": 90000}, r).classificacao \
        == "passa_alerta"
    # com passa_min 80 mil, a faixa 60-100 mil cruza o corte: pede refino
    assert triagem.avaliar(TODOS_OK, r).classificacao == "em_andamento"


# ── janela e cadência (puro) ─────────────────────────────────────────────
@pytest.mark.parametrize("quando,esperado", [
    (utc(2026, 10, 20, 13, 0), utc(2026, 10, 20, 13, 0)),      # terça 10h SP: dentro
    (utc(2026, 10, 23, 20, 59), utc(2026, 10, 23, 20, 59)),    # sexta 17:59: dentro
    (utc(2026, 10, 23, 21, 0), utc(2026, 10, 26, 12, 0)),      # sexta 18:00: segunda 9h
    (utc(2026, 10, 24, 15, 0), utc(2026, 10, 26, 12, 0)),      # sábado: segunda 9h
    (utc(2026, 10, 25, 23, 0), utc(2026, 10, 26, 12, 0)),      # domingo 20h: segunda 9h
    (utc(2026, 10, 26, 11, 59), utc(2026, 10, 26, 12, 0)),     # segunda 8:59: 9h do mesmo dia
    (utc(2026, 10, 21, 2, 0), utc(2026, 10, 21, 12, 0)),       # terça 23h SP: quarta 9h
])
def test_proxima_janela_seg_sex_9_18(quando, esperado):
    assert triagem.proxima_janela(quando, SP, JANELA) == esperado


def test_janela_usa_o_fuso_da_agencia():
    # 13:30 UTC = 10:30 em SP, 9:30 em Manaus, 8:30 em Rio Branco
    assert triagem.na_janela(utc(2026, 10, 20, 13, 30), "America/Manaus", JANELA)
    assert not triagem.na_janela(utc(2026, 10, 20, 13, 30), "America/Rio_Branco", JANELA)
    assert triagem.proxima_janela(utc(2026, 10, 20, 13, 30), "America/Rio_Branco", JANELA) == utc(2026, 10, 20, 14, 0)


def test_cadencia_5_toques_a_partir_do_toque_1():
    cad, t1 = triagem.CADENCIA_PADRAO, AGORA                       # terça 10h
    venc, ultimo = [], t1
    for k in range(1, 5):
        ultimo = triagem.vencimento_toque(cad, k, t1, ultimo, SP)
        venc.append(ultimo)
    assert venc == [AGORA + timedelta(hours=2), AGORA + timedelta(days=1), AGORA + timedelta(days=3),
                    AGORA + timedelta(days=7)]
    assert [t["tipo"] for t in cad["toques"]] == ["inicial", "retoma", "pergunta_facil", "valor", "fechamento"]


def test_cadencia_empurrada_pela_janela_nao_encurta_o_espaco_entre_toques():
    cad, t1 = triagem.CADENCIA_PADRAO, utc(2026, 10, 23, 20, 0)   # sexta 17h SP
    t2 = triagem.vencimento_toque(cad, 1, t1, t1, SP)
    assert t2 == utc(2026, 10, 26, 12, 0)                          # +2h = 19h → segunda 9h
    t3 = triagem.vencimento_toque(cad, 2, t1, t2, SP)
    assert t3 == utc(2026, 10, 27, 12, 0)                          # sábado → segunda 9h + 22h = terça 7h → 9h


def test_cadencia_da_config_e_validacao(conn):
    conn.execute("truncate radar.config")
    conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, 'triagem_cadencia', %s::jsonb)""",
                 (AGENCIA, json.dumps({"toques": [{"apos_horas": 0, "tipo": "inicial"}, {"apos_horas": 4, "tipo": "retoma"}],
                                       "janela": {"dias": [0, 1, 2, 3, 4, 5], "inicio": "08:00", "fim": "12:00"},
                                       "tipos": {"retoma": "Retome falando do {segmento}."}})))
    c = triagem.cadencia(conn, AGENCIA)
    assert [t["apos_horas"] for t in c["toques"]] == [0, 4] and c["janela"]["dias"] == [0, 1, 2, 3, 4, 5]
    assert c["tipos"]["retoma"] == "Retome falando do {segmento}." and c["tipos"]["valor"] == triagem.INSTRUCOES_TOQUE["valor"]
    conn.execute("""update radar.config set valor = %s::jsonb where chave = 'triagem_cadencia'""",
                 (json.dumps({"toques": [{"apos_horas": 5, "tipo": "x"}], "janela": {"inicio": "18:00", "fim": "09:00"}}),))
    assert triagem.cadencia(conn, AGENCIA)["toques"] == triagem.CADENCIA_PADRAO["toques"]       # 1º toque precisa ser 0
    assert triagem.cadencia(conn, AGENCIA)["janela"] == JANELA


# ── roteiro e formulário (puro) ──────────────────────────────────────────
def test_roteiro_na_ordem_cidade_cedo_e_faturamento_por_ultimo():
    ordem, fatos = [], {}
    valores = {"situacao": {"segmento": "loja"}, "cidade": {"cidade": "Teresina"}, "marketing": {"investe_anuncio": "nao"},
               "necessidade": {"necessidade_clara": "sim"}, "experiencia_agencia": {"experiencia_agencia": "nunca teve"},
               "autoridade": {"decisor": "nao_decide"}, "contato_decisor": {"contato_decisor": "Ana"},
               "timing": {"prazo_inicio": "30_60d"}, "faturamento": {"faixa_faturamento": "30_60k"},
               "refino": {"faixa_refino": "perto_do_maior"}}
    while True:
        etapa, _ = triagem.proxima_etapa(fatos, R)
        if etapa is None:
            break
        ordem.append(etapa)
        fatos.update(valores[etapa])
    assert ordem == ["situacao", "cidade", "marketing", "necessidade", "experiencia_agencia", "autoridade",
                     "contato_decisor", "timing", "faturamento", "refino"]


def test_lp_nao_repete_o_que_veio_no_formulario_e_usa_para_aprofundar():
    respostas = [{"pergunta": "Qual o seu segmento?", "resposta": "Clínica odontológica"},
                 {"pergunta": "Qual o faturamento mensal da empresa?", "resposta": "Entre R$ 30 e 60 mil"},
                 {"pergunta": "Cidade", "resposta": ""}]                                       # vazio não cobre
    cobertos = triagem.temas_do_formulario(respostas)
    assert set(cobertos) == {"situacao", "faturamento"}
    assert triagem.proxima_etapa({}, R, cobertos) == ("cidade", False)                      # pula o segmento
    t = triagem.tarefa({}, R, triagem.ROTEIRO_PADRAO, cobertos, "Responda.")
    assert "NÃO pergunte de novo" in t and "Entre R$ 30 e 60 mil" in t and "Próximo assunto: cidade" in t
    # tudo o que o formulário não cobre já foi respondido: aprofunda o que veio do formulário, sem repetir
    resto = {"cidade": "Teresina", "investe_anuncio": "sim", "necessidade_clara": "sim",
             "experiencia_agencia": "nunca teve", "decisor": "dono_socio", "prazo_inicio": "ate_30d"}
    assert triagem.proxima_etapa(resto, R, cobertos) == ("situacao", True)
    t = triagem.tarefa(resto, R, triagem.ROTEIRO_PADRAO, cobertos, "Responda.")
    assert "não repita a pergunta" in t and "Clínica odontológica" in t
    assert triagem.ROTEIRO_PADRAO["situacao"] not in t


# ── o que a IA não trata (puro) ──────────────────────────────────────────
@pytest.mark.parametrize("texto,proibido", [
    ("O Opala custa R$ 2.500 por mês.", True),
    ("O investimento mínimo é R$ 3 mil.", True),
    ("Consigo 10% de desconto para você!", True),
    ("Somos melhores que a concorrência.", False),
    ("Somos melhor que a agência X.", True),
    ("Vi que seu faturamento está na faixa dos R$ 40 mil. Há quanto tempo vocês estão no mercado?", False),
    ("O valor depende do caso; o consultor mostra na conversa.", False),
    ("Desconto e contrato o consultor trata com você na conversa.", False),
])
def test_assunto_proibido(texto, proibido):
    assert triagem.assunto_proibido(texto) is proibido


def test_prompt_v2_tem_as_regras_do_que_a_ia_nao_trata():
    s = ia.montar_sistema("RCO HUB", "Pode dar 20% de desconto.", origem="lp", v2=True)
    assert "piso do Opala" in s and "depende do caso" in s and "concorrentes" in s and "contratos" in s
    assert s.index("Instruções da agência") < s.index("Regras que valem sempre")
    assert "POR ÚLTIMO, faturamento em faixas" in s and "mais perto dos 30 ou dos 60" in s


def test_schema_estrito_cobre_os_fatos_novos():
    sch = ia.schema_estrito(ia.RespostaIA)
    fatos = sch["properties"]["fatos"]
    assert fatos["additionalProperties"] is False and set(fatos["required"]) == set(ia.Fatos.model_fields)
    assert {"faixa_faturamento", "faixa_refino", "orcamento_ou_intencao", "assuntos_comerciais"} <= set(fatos["required"])


def test_resumo_ao_sdr_tem_todos_os_campos():
    fatos = {**TODOS_OK, "faturamento_valor": 75000, "valor_anuncio": "R$ 2 mil/mês",
             "como_atrai_clientes": "Instagram e indicação", "objecoes": ["já teve agência ruim"],
             "assuntos_comerciais": ["preco"]}
    res = triagem.avaliar(fatos, R)
    txt = triagem.resumo_sdr({"nome": "Ana", "empresa": "Sorriso", "telefone": "+5586999990000"}, fatos, res,
                             "http://c/inbox?c=1", {"rotulo": "quarta (21/10) às 10h"}, "Lead animada.")
    for trecho in ("Nome: Ana", "Empresa: Sorriso", "Telefone: +5586999990000", "Segmento: Clínica odontológica",
                   "Cidade: Teresina", "Faturamento: R$ 75 mil", "valor dito bate com a faixa",
                   "Investimento em anúncio: sim; quanto: R$ 2 mil/mês", "Marketing e conteúdo hoje: Instagram",
                   "Dor/objetivo (palavras do lead): agenda vazia", "Decisor: dono_socio",
                   "Quando quer começar: ate_30d", "Experiência com agência: nunca teve",
                   "Objeções: já teve agência ruim", "Perguntou sobre (o consultor trata): preco",
                   "Classificação: Passa direto. Motivo: todos os portões ok", "Conversa: http://c/inbox?c=1",
                   "Horário escolhido: quarta (21/10) às 10h", "Resumo da IA: Lead animada."):
        assert trecho in txt, trecho


# ── banco: stub HTTP do COMERCIAL ────────────────────────────────────────
class StubComercial:
    """Cliente real (radar.comercial.Comercial) sobre MockTransport. /enviar grava a mensagem no inbox como o
    COMERCIAL; /horarios e /agendar devolvem as respostas da fila (a última se repete); "500" = HTTP 500."""

    def __init__(self, conn, horarios=None, agendar=None):
        self.conn, self.chamadas = conn, []
        self.fila = {"horarios": list(horarios or [{"ok": True, "sdr_id": SDR, "opcoes": OPCOES}]),
                     "agendar": list(agendar or [{"ok": True, "evento_id": "ev1", "lead_call_id": "lc1",
                                                  "meet_url": "https://meet.google.com/abc"}])}
        self.cliente = Comercial("http://comercial", "s", client=httpx.AsyncClient(transport=httpx.MockTransport(self._h)))

    def _h(self, req: httpx.Request):
        rota, corpo = req.url.path.rsplit("/", 1)[-1], json.loads(req.content)
        self.chamadas.append((rota, corpo))
        if rota == "enviar":
            cid = corpo["conversa_id"]
            conv_id, contato = self.conn.execute("select conversation_id, contact_id from radar.conversas where id = %s",
                                                 (cid,)).fetchone()
            if conv_id is None:
                conv_id = self.conn.execute("insert into public.conversations (contact_id) values (%s) returning id",
                                            (contato,)).fetchone()[0]
                self.conn.execute("update radar.conversas set conversation_id = %s where id = %s", (conv_id, cid))
            self.conn.execute("""insert into public.messages (conversation_id, sender_type, content_text, created_at)
                                 values (%s, 'bot', %s, %s)""", (conv_id, corpo["texto"], AGORA))
            return httpx.Response(200, json={"ok": True, "conversation_id": str(conv_id), "contact_id": str(contato)})
        if rota in self.fila:
            r = self.fila[rota].pop(0) if len(self.fila[rota]) > 1 else self.fila[rota][0]
            return httpx.Response(500) if r == "500" else httpx.Response(200, json=r)
        return httpx.Response(200, json={"ok": True})

    def de(self, rota):
        return [c for r, c in self.chamadas if r == rota]


def resp(mensagem="Legal! E em qual cidade vocês atendem?", intencao="continuar", escolha="indefinido", **fatos):
    return ia.RespostaIA(mensagem=mensagem, campos=ia.CamposColetados(), intencao=intencao, resumo="Resumo da IA.",
                         fatos=ia.Fatos(**fatos), escolha_horario=escolha)


async def lp_contatada(conn, quando=AGORA, **stub):
    [cid] = preparar_lp(conn)
    conn.execute("update radar.conversas set contexto = contexto || %s::jsonb where id = %s",
                 (json.dumps({"nome": "Ana", "empresa": "Sorriso", "telefone": "+5586999990000"}), cid))
    com = StubComercial(conn, **stub)
    assert await conversa.despachar(conn, com.cliente, quando) == 1
    return cid, com


async def responde(conn, com, cid, texto, quando, *respostas):
    lead_lp_responde(conn, cid, texto, quando)
    fake = FakeIA(*respostas)
    await conversa.conversar(conn, com.cliente, quando + timedelta(seconds=5), cliente_ia=fake)
    return fake


def textos(com):
    return [e["texto"] for e in com.de("enviar")]


# ── banco: passa_direto → horários → escolha → agendar → passar ──────────
async def test_passa_direto_oferece_2_horarios_agenda_a_escolha_e_passa_ao_sdr(conn):
    cid, com = await lp_contatada(conn)
    t = AGORA + timedelta(minutes=2)
    await responde(conn, com, cid, "Sou a dona, Teresina, faturo 80 mil, quero começar já", t, resp(**TODOS_OK))
    [h] = com.de("horarios")
    assert h["conversa_id"] == str(cid) and h["lead_id"] is None and h["duracao_min"] == 30
    oferta = textos(com)[-1]
    assert OPCOES[0]["rotulo"] in oferta and OPCOES[1]["rotulo"] in oferta
    c = conv(conn, cid)
    assert (c["estado"], c["etapa"], c["decisao"]) == ("ia", "oferta", "passa_direto")
    assert c["agenda"]["sdr_id"] == SDR and c["agenda"]["opcoes"] == OPCOES and com.de("passar") == []

    fake = await responde(conn, com, cid, "Pode ser quinta", t + timedelta(minutes=5),
                          resp("Ótimo!", escolha="opcao_2"))
    assert "quinta (22/10) às 15h" in fake.chamadas[0]["messages"][-1]["content"]      # IA interpreta com as opções
    [a] = com.de("agendar")
    assert (a["sdr_id"], a["inicio"], a["fim"]) == (SDR, OPCOES[1]["inicio"], OPCOES[1]["fim"])
    assert a["idempotency_key"] == f"radar-agendar-{cid}-{OPCOES[1]['inicio']}" and a["conversa_id"] == str(cid)
    assert "quinta (22/10) às 15h" in textos(com)[-1]                                  # confirmação ao lead
    [p] = com.de("passar")
    assert (p["motivo"], p["classificacao"], p["alertas"]) == ("qualificado", "passa_direto", [])
    assert p["agendamento"]["lead_call_id"] == "lc1" and p["agendamento"]["rotulo"] == "quinta (22/10) às 15h"
    assert "Horário escolhido: quinta (22/10) às 15h" in p["resumo"] and "Nome: Ana" in p["resumo"]
    assert "Classificação: Passa direto" in p["resumo"] and "http://comercial/inbox?c=" in p["resumo"]
    assert p["campos"]["decisor"] == "dono_socio"
    c = conv(conn, cid)
    assert (c["estado"], c["motivo_fim"]) == ("humano", "qualificado") and c["agenda"]["escolhido"]["evento_id"] == "ev1"


async def test_horario_ocupado_oferece_de_novo_uma_vez(conn):
    cid, com = await lp_contatada(conn, horarios=[{"ok": True, "sdr_id": SDR, "opcoes": OPCOES},
                                                  {"ok": True, "sdr_id": SDR, "opcoes": OPCOES_NOVAS}],
                                  agendar=[{"ok": False, "motivo": "horario_indisponivel"},
                                           {"ok": True, "evento_id": "ev2", "lead_call_id": "lc2"}])
    t = AGORA + timedelta(minutes=2)
    await responde(conn, com, cid, "Tudo certo", t, resp(**TODOS_OK))
    await responde(conn, com, cid, "A primeira", t + timedelta(minutes=5), resp(escolha="opcao_1"))
    assert len(com.de("horarios")) == 2 and "acabou de ser preenchido" in textos(com)[-1]
    assert OPCOES_NOVAS[0]["rotulo"] in textos(com)[-1] and com.de("passar") == []
    assert conv(conn, cid)["agenda"]["tentativas"] == 2
    await responde(conn, com, cid, "Segunda às 14h", t + timedelta(minutes=9), resp(escolha="opcao_2"))
    assert com.de("agendar")[-1]["inicio"] == OPCOES_NOVAS[1]["inicio"]
    assert com.de("passar")[-1]["agendamento"]["lead_call_id"] == "lc2"


async def test_horario_ocupado_duas_vezes_passa_sem_agendar(conn):
    cid, com = await lp_contatada(conn, agendar=[{"ok": False, "motivo": "horario_indisponivel"}])
    t = AGORA + timedelta(minutes=2)
    await responde(conn, com, cid, "Tudo certo", t, resp(**TODOS_OK))
    await responde(conn, com, cid, "A primeira", t + timedelta(minutes=5), resp(escolha="opcao_1"))
    await responde(conn, com, cid, "Então a outra", t + timedelta(minutes=9), resp(escolha="opcao_2"))
    assert len(com.de("horarios")) == 2 and len(com.de("agendar")) == 2
    [p] = com.de("passar")
    assert p["agendamento"] is None and "horario_indisponivel" in p["resumo"] and p["classificacao"] == "passa_direto"
    assert conv(conn, cid)["estado"] == "humano"


@pytest.mark.parametrize("horarios,motivo", [
    ({"ok": False, "motivo": "sem_google"}, "sem_google"),
    ({"ok": False, "motivo": "sem_horario"}, "sem_horario"),
    ({"ok": True, "sdr_id": SDR, "opcoes": OPCOES[:1]}, "sem_horario"),               # só 1 opção
    ({"ok": False, "motivo": "sem_google", "sdr_id": SDR, "opcoes": OPCOES}, "sem_google"),   # ok false manda
    ("500", "erro"),                                                                   # COMERCIAL fora do ar
])
async def test_sem_google_ou_sem_horario_passa_sem_oferecer(conn, horarios, motivo):
    cid, com = await lp_contatada(conn, horarios=[horarios])
    await responde(conn, com, cid, "Tudo certo", AGORA + timedelta(minutes=2), resp(**TODOS_OK))
    # sem oferta: nenhum dia/hora é prometido; a mensagem diz que o time chama para combinar
    assert com.de("agendar") == [] and textos(com)[-1] == triagem.MENSAGENS_PADRAO["lp"]["passar_sem_horario"]
    assert "combinar o melhor horário" in textos(com)[-1]
    assert all(o["rotulo"] not in t for o in OPCOES for t in textos(com))
    [p] = com.de("passar")
    assert (p["classificacao"], p["agendamento"]) == ("passa_direto", None)
    assert f"horário não oferecido ({motivo}" in p["resumo"] and "Horário escolhido: nenhum" in p["resumo"]
    c = conv(conn, cid)
    assert (c["estado"], c["etapa"]) == ("humano", "qualificando")


async def test_nenhum_horario_serve_passa_sem_agendar(conn):
    cid, com = await lp_contatada(conn)
    t = AGORA + timedelta(minutes=2)
    await responde(conn, com, cid, "Tudo certo", t, resp(**TODOS_OK))
    await responde(conn, com, cid, "Nenhum dá", t + timedelta(minutes=5), resp(escolha="nenhuma"))
    assert com.de("agendar") == []
    assert "nenhum dos 2 horários" in com.de("passar")[0]["resumo"]


async def test_resposta_sem_escolha_continua_na_oferta(conn):
    cid, com = await lp_contatada(conn)
    t = AGORA + timedelta(minutes=2)
    await responde(conn, com, cid, "Tudo certo", t, resp(**TODOS_OK))
    await responde(conn, com, cid, "Vai ter link?", t + timedelta(minutes=5),
                   resp("Vai sim! Qual dos dois horários fica melhor?"))
    c = conv(conn, cid)
    assert c["etapa"] == "oferta" and com.de("agendar") == [] and com.de("passar") == []
    assert textos(com)[-1] == "Vai sim! Qual dos dois horários fica melhor?"


# ── banco: passa_alerta, redireciona, desqualifica ───────────────────────
async def test_passa_alerta_passa_sem_oferecer_horario(conn):
    cid, com = await lp_contatada(conn)
    await responde(conn, com, cid, "Somos de Timon", AGORA + timedelta(minutes=2), resp(**{**TODOS_OK, "cidade": "Timon"}))
    assert com.de("horarios") == []
    [p] = com.de("passar")
    assert p["classificacao"] == "passa_alerta" and "Timon: avaliação comercial" in p["alertas"][0]
    assert "Alertas: Timon" in p["resumo"] and "Classificação: Passa com alerta" in p["resumo"]
    c = conv(conn, cid)
    assert (c["decisao"], c["estado"]) == ("passa_alerta", "humano")
    assert c["nota_detalhe"]["versao"] == 2 and "Timon" in c["nota_detalhe"]["justificativa"]


async def test_redireciona_passa_com_alerta_trafego_isolado(conn):
    cid, com = await lp_contatada(conn)
    await responde(conn, com, cid, "Faturo uns 20 mil", AGORA + timedelta(minutes=2),
                   resp(**{**TODOS_OK, "faixa_faturamento": "ate_30k"}))
    assert com.de("horarios") == []
    [p] = com.de("passar")
    assert p["classificacao"] == "redireciona" and p["alertas"][0].startswith("tráfego isolado")
    assert conv(conn, cid)["decisao"] == "redireciona"


async def test_desqualifica_encerra_com_a_justificativa(conn):
    cid, com = await lp_contatada(conn)
    await responde(conn, com, cid, "Marketing pra mim é gasto", AGORA + timedelta(minutes=2),
                   resp(ve_marketing_como_despesa="sim", segmento="loja"))
    assert com.de("passar") == [] and textos(com)[-1] == triagem.MENSAGENS_PADRAO["lp"]["descartar"]
    [d] = com.de("descartar")
    assert d["conversa_id"] == str(cid) and d["motivo"] == "Desqualificado: eliminatório: vê marketing como despesa"
    c = conv(conn, cid)
    assert (c["estado"], c["motivo_fim"], c["decisao"]) == ("encerrada", "desqualificado", "desqualifica")


async def test_fatos_somam_entre_respostas_e_decidem_so_quando_completos(conn):
    cid, com = await lp_contatada(conn)
    t = AGORA + timedelta(minutes=2)
    metade = {k: TODOS_OK[k] for k in ("segmento", "cidade", "investe_anuncio", "necessidade_clara")}
    resto = {k: v for k, v in TODOS_OK.items() if k not in metade}
    await responde(conn, com, cid, "Clínica em Teresina", t, resp("E quem decide aí?", **metade))
    c = conv(conn, cid)
    assert c["decisao"] == "em_andamento" and c["estado"] == "ia" and textos(com)[-1] == "E quem decide aí?"
    assert c["proximo_followup_em"] == t + timedelta(seconds=5, hours=2)             # resposta nossa = toque 1
    await responde(conn, com, cid, "Eu decido...", t + timedelta(minutes=5), resp(**resto))
    assert conv(conn, cid)["etapa"] == "oferta" and len(com.de("horarios")) == 1


async def test_lp_tarefa_nao_repete_o_que_veio_no_formulario(conn):
    cid, com = await lp_contatada(conn)                    # formulário: segmento e investimento em anúncio
    fake = await responde(conn, com, cid, "Oi, pode falar", AGORA + timedelta(minutes=2), resp())
    tarefa = fake.chamadas[0]["messages"][-1]["content"]
    assert "Já respondido no formulário (NÃO pergunte de novo" in tarefa and "Clínica odontológica" in tarefa
    assert "Próximo assunto: cidade" in tarefa and "Próximo assunto: situacao" not in tarefa
    assert "Próximo assunto: marketing" not in tarefa


async def test_ia_falando_preco_nao_sai_e_passa_ao_humano(conn):
    cid, com = await lp_contatada(conn)
    await responde(conn, com, cid, "Quanto custa?", AGORA + timedelta(minutes=2),
                   resp("O Opala custa R$ 2.500 por mês.", assuntos_comerciais=["preco"]))
    assert len(com.de("enviar")) == 1                                                  # só a 1ª mensagem
    [p] = com.de("passar")
    assert p["motivo"] == "ia_sem_resposta" and "preço, desconto ou concorrente" in p["resumo"]
    assert "Perguntou sobre (o consultor trata): preco" in p["resumo"]               # o SDR sabe o que foi perguntado
    assert conv(conn, cid)["estado"] == "humano"


async def test_pediu_humano_passa_com_resumo_completo(conn):
    cid, com = await lp_contatada(conn)
    await responde(conn, com, cid, "Quero falar com uma pessoa", AGORA + timedelta(minutes=2),
                   resp("Claro, já chamo alguém!", "pediu_humano", cidade="Teresina"))
    [p] = com.de("passar")
    assert p["motivo"] == "pediu_humano" and "Cidade: Teresina" in p["resumo"] and "classificacao" not in p


# ── banco: cadência de 5 toques ──────────────────────────────────────────
async def test_cadencia_5_toques_com_tipo_de_cada_um_e_encerra(conn):
    cid, com = await lp_contatada(conn)                                     # toque 1: terça 10h SP
    conn.execute("""update radar.conversas set nota_detalhe = '{"fatos": {"segmento": "odontologia"}}' where id = %s""",
                 (cid,))
    vencimentos = [AGORA + timedelta(hours=2), AGORA + timedelta(days=1), AGORA + timedelta(days=3),
                   AGORA + timedelta(days=7)]
    tipos = ["retoma", "pergunta_facil", "valor", "fechamento"]
    for venc, tipo in zip(vencimentos, tipos):
        assert conv(conn, cid)["proximo_followup_em"] == venc
        fake = FakeIA(resp(f"toque {tipo}"))
        await conversa.conversar(conn, com.cliente, venc - timedelta(minutes=1), cliente_ia=fake)
        assert fake.chamadas == []                                          # ainda não venceu
        await conversa.conversar(conn, com.cliente, venc, cliente_ia=fake)
        tarefa = fake.chamadas[0]["messages"][-1]["content"]
        assert triagem.INSTRUCOES_TOQUE[tipo].split("{")[0][:40] in tarefa
        assert textos(com)[-1] == f"toque {tipo}"
    c = conv(conn, cid)
    assert c["followups_enviados"] == 4 and c["proximo_followup_em"] == AGORA + timedelta(days=9)   # +48 h (quinta)
    await conversa.conversar(conn, com.cliente, AGORA + timedelta(days=9), cliente_ia=FakeIA())
    c = conv(conn, cid)
    assert (c["estado"], c["motivo_fim"]) == ("encerrada", "sem_resposta") and len(com.de("enviar")) == 5


async def test_toque_valor_fala_do_segmento(conn):
    cid, com = await lp_contatada(conn)
    conn.execute("""update radar.conversas set followups_enviados = 2, proximo_followup_em = %s,
                    nota_detalhe = '{"fatos": {"segmento": "odontologia"}}' where id = %s""",
                 (AGORA + timedelta(days=3), cid))
    fake = FakeIA(resp("ideia"))
    await conversa.conversar(conn, com.cliente, AGORA + timedelta(days=3), cliente_ia=fake)
    assert "ligado ao segmento dele (odontologia)" in fake.chamadas[0]["messages"][-1]["content"]


async def test_cadencia_nao_sai_fora_da_janela_e_empurra(conn):
    cid, com = await lp_contatada(conn)
    sabado = utc(2026, 10, 24, 14, 0)                                       # sábado 11h SP (dentro de 8h-20h da agência)
    conn.execute("update radar.conversas set proximo_followup_em = %s where id = %s", (sabado, cid))
    fake = FakeIA(resp("oi"))
    await conversa.conversar(conn, com.cliente, sabado, cliente_ia=fake)
    assert fake.chamadas == [] and len(com.de("enviar")) == 1
    assert conv(conn, cid)["proximo_followup_em"] == utc(2026, 10, 26, 12, 0)          # segunda 9h SP


async def test_toque_1_de_lp_espera_dia_util(conn):
    [cid] = preparar_lp(conn)
    com = StubComercial(conn)
    sabado = utc(2026, 10, 24, 14, 0)
    assert await conversa.despachar(conn, com.cliente, sabado) == 0 and com.de("enviar") == []
    sexta_tarde = utc(2026, 10, 23, 20, 30)                                 # sexta 17:30 SP
    assert await conversa.despachar(conn, com.cliente, sexta_tarde) == 1
    assert conv(conn, cid)["proximo_followup_em"] == utc(2026, 10, 26, 12, 0)          # +2h cai na segunda 9h


async def test_fim_da_cadencia_com_bom_perfil_vai_para_a_esteira(conn):
    cid, com = await lp_contatada(conn)
    fatos = {"faixa_faturamento": "acima_100k", "segmento": "odontologia"}
    conn.execute("""update radar.conversas set followups_enviados = 4, proximo_followup_em = %s,
                    nota_detalhe = %s::jsonb where id = %s""", (AGORA + timedelta(days=9), json.dumps({"fatos": fatos}), cid))
    await conversa.conversar(conn, com.cliente, AGORA + timedelta(days=9), cliente_ia=FakeIA())
    c = conv(conn, cid)
    assert (c["estado"], c["etapa"], c["decisao"]) == ("ia", "esteira", "esteira")
    assert c["proximo_followup_em"] == utc(2026, 11, 30, 12, 0)    # quinta 29/10 + 30 d = sábado → segunda 9h


async def test_fim_da_cadencia_na_oferta_passa_sem_horario(conn):
    cid, com = await lp_contatada(conn)
    await responde(conn, com, cid, "Tudo certo", AGORA + timedelta(minutes=2), resp(**TODOS_OK))
    conn.execute("update radar.conversas set followups_enviados = 4, proximo_followup_em = %s where id = %s",
                 (AGORA + timedelta(days=9), cid))
    await conversa.conversar(conn, com.cliente, AGORA + timedelta(days=9), cliente_ia=FakeIA())
    [p] = com.de("passar")
    assert p["classificacao"] == "passa_direto" and "não respondeu à oferta" in p["resumo"]


# ── banco: esteira ───────────────────────────────────────────────────────
async def test_esteira_30_60_90_dias_com_valor_e_encerra_sem_resposta(conn):
    cid, com = await lp_contatada(conn)
    t = AGORA + timedelta(minutes=2)                                        # terça
    await responde(conn, com, cid, "Só lá pro ano que vem", t, resp(**{**TODOS_OK, "prazo_inicio": "mais_60d"}))
    assert textos(com)[-1] == triagem.MENSAGENS_PADRAO["lp"]["esteira"] and com.de("passar") == []
    c = conv(conn, cid)
    assert (c["etapa"], c["decisao"], c["esteira_enviados"]) == ("esteira", "esteira", 0)
    inicio = c["esteira_desde"]
    for n in (1, 2, 3):
        alvo = triagem.proxima_janela(inicio + timedelta(days=30 * n), SP, JANELA)
        assert conv(conn, cid)["proximo_followup_em"] == alvo
        assert triagem.na_janela(alvo, SP, JANELA)
        fake = FakeIA(resp(f"valor {n}"))
        await conversa.conversar(conn, com.cliente, alvo, cliente_ia=fake)
        tarefa = fake.chamadas[0]["messages"][-1]["content"]
        assert "Nunca pergunte se ainda tem interesse" in tarefa and "Clínica odontológica" in tarefa
        assert conv(conn, cid)["esteira_enviados"] == n and textos(com)[-1] == f"valor {n}"
    fim = conv(conn, cid)["proximo_followup_em"]
    await conversa.conversar(conn, com.cliente, fim, cliente_ia=FakeIA())
    c = conv(conn, cid)
    assert (c["estado"], c["motivo_fim"]) == ("encerrada", "sem_resposta")
    assert fim - inicio < timedelta(days=100)


async def test_resposta_na_esteira_retoma_a_qualificacao(conn):
    cid, com = await lp_contatada(conn)
    t = AGORA + timedelta(minutes=2)
    await responde(conn, com, cid, "Só daqui a uns meses", t, resp(**{**TODOS_OK, "prazo_inicio": "mais_60d"}))
    volta = t + timedelta(days=40)
    fake = await responde(conn, com, cid, "Oi! Agora quero começar", volta, resp("Que bom! Quando quer começar?"))
    tarefa = fake.chamadas[0]["messages"][-1]["content"]
    assert "retome a conversa de onde parou" in tarefa and "Próximo assunto: timing" in tarefa
    c = conv(conn, cid)
    assert (c["etapa"], c["decisao"], c["estado"]) == ("qualificando", "em_andamento", "ia")
    assert c["nota_detalhe"]["fatos"]["prazo_inicio_anterior"] == "mais_60d"
    await responde(conn, com, cid, "Mês que vem", volta + timedelta(minutes=5), resp(prazo_inicio="ate_30d"))
    assert conv(conn, cid)["etapa"] == "oferta"                                        # agora passa direto


# ── origem do Radar: v2 só quando a agência inclui 'radar' ───────────────
async def test_radar_entra_na_v2_quando_a_agencia_inclui(conn):
    from tests.test_conversa import FakeComercial, lead_responde, preparar
    leads, _ = preparar(conn, n_leads=1)
    conn.execute("truncate radar.config")
    conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, 'triagem_regras', %s::jsonb)""",
                 (AGENCIA, json.dumps({"origens": ["lp", "radar"]})))
    com = FakeComercial(conn)
    await conversa.despachar(conn, com, AGORA)
    assert conn.execute("select proximo_followup_em from radar.conversas").fetchone()[0] == AGORA + timedelta(hours=2)
    lead_responde(conn, leads[0], "Eu decido", AGORA + timedelta(minutes=2))
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3),
                             cliente_ia=FakeIA(resp(**{**TODOS_OK, "ve_marketing_como_despesa": "sim"})))
    st, txt = conn.execute("select status, motivo_descarte_texto from radar.leads").fetchone()
    assert st == "descartado" and "vê marketing como despesa" in txt
    assert conn.execute("select decisao, motivo_fim from radar.conversas").fetchone() == ("desqualifica", "desqualificado")


# ── Radar na v2 por padrão (18/11/2026) ──────────────────────────────────
def test_v2_vale_para_lp_e_radar_por_padrao():
    assert R["origens"] == ["lp", "radar"]
    assert triagem.v2_ativa(R, "radar") and triagem.v2_ativa(R, "lp")
    assert not triagem.v2_ativa({**R, "origens": ["lp"]}, "radar")


def test_regras_da_agencia_tiram_o_radar_da_v2(conn):
    assert triagem.v2_ativa(triagem.regras(conn, AGENCIA), "radar")                     # sem config: padrão
    conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, 'triagem_regras', %s::jsonb)""",
                 (AGENCIA, json.dumps({"origens": ["lp"]})))
    assert not triagem.v2_ativa(triagem.regras(conn, AGENCIA), "radar")
    assert triagem.v2_ativa(triagem.regras(conn, AGENCIA), "lp")


def test_fatos_da_ficha_so_dado_objetivo():
    from datetime import date
    lead = {"cidade": "Teresina", "categoria": "Barbearia", "nicho": None, "faturamento_estimado_faixa": "80k_200k"}
    assert triagem.fatos_da_ficha(lead, {"abertura": date(2015, 3, 1)}) == {
        "cidade": "Teresina", "segmento": "Barbearia", "tempo_mercado": "CNPJ aberto em 2015"}
    assert triagem.fatos_da_ficha({"cidade": "", "categoria": None}) == {}
    # o que o lead disser na conversa vale por cima da ficha
    assert triagem.juntar_fatos(triagem.fatos_da_ficha(lead), {"cidade": "Timon"})["cidade"] == "Timon"


def test_prompt_v2_nao_deixa_a_ia_propor_horario_nem_repetir_a_ficha():
    s = ia.montar_sistema("RCO HUB", None, origem="radar", v2=True)
    assert "nunca proponha dia ou horário de reunião" in s
    assert "ficha do negócio (Maps, CNPJ, site) NÃO se pergunta de novo" in s
    assert "encontrou no Google Maps" in s and "—" not in s and "–" not in s


class ComercialRadar:
    """FakeComercial do fluxo do Radar + rotas de agenda (respostas fixas)."""

    url = "http://comercial"

    def __init__(self, conn, horarios=None):
        from tests.test_conversa import FakeComercial
        self.base = FakeComercial(conn)
        self.envios, self.passagens = self.base.envios, self.base.passagens
        self.h = horarios or {"ok": False, "motivo": "sem_google"}
        self.pedidos, self.agendamentos = [], []

    async def enviar(self, **kw):
        return await self.base.enviar(**kw)

    async def passar(self, **kw):
        return await self.base.passar(**kw)

    async def horarios(self, **kw):
        self.pedidos.append(kw)
        return self.h

    async def agendar(self, **kw):
        self.agendamentos.append(kw)
        return {"ok": True, "evento_id": "ev", "lead_call_id": "lc"}


def preparar_radar_v2(conn):
    """Lead do Radar com ficha (categoria do Maps, CNPJ escolhido aberto em 2015) e a agência SEM opt-out."""
    from tests.test_conversa import preparar
    leads, _ = preparar(conn, n_leads=1)
    conn.execute("delete from radar.config where chave = 'triagem_regras'")             # volta ao padrão
    conn.execute("update radar.leads set categoria = 'Barbearia' where id = %s", (leads[0],))
    conn.execute("""insert into radar.empresas (agency_id, lead_id, escolhido, cnpj, razao_social, abertura, porte,
                        confianca_cnpj, motivo_confianca, fonte)
                    values (%s, %s, true, '11111111000111', 'BARBEARIA ZERO LTDA', '2015-03-01', 'ME', 80, 't', 't')""",
                 (AGENCIA, leads[0]))
    return leads[0]


async def test_radar_usa_v2_por_padrao_com_a_abordagem_como_toque_1_e_a_ficha_como_fatos(conn):
    from tests.test_conversa import lead_responde
    lead = preparar_radar_v2(conn)
    com = ComercialRadar(conn)
    assert await conversa.despachar(conn, com, AGORA) == 1
    # toque 1 = a abordagem da agência (não um texto da triagem); toque 2 da cadência v2 em +2 h
    assert com.envios[0]["texto"] == "Oi, falo com o responsável pela Barbearia 0? Vi que vocês têm nota 4,6 no Google."
    assert conn.execute("select proximo_followup_em from radar.conversas").fetchone()[0] == AGORA + timedelta(hours=2)
    lead_responde(conn, lead, "Sou eu, pode falar", AGORA + timedelta(minutes=2))
    fake = FakeIA(resp("Legal! Hoje como os clientes chegam até vocês?", decisor="dono_socio"))
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3), cliente_ia=fake)
    ch = fake.chamadas[0]
    assert "seguindo um roteiro" in ch["system"] and "encontrou no Google Maps" in ch["system"]
    ficha = ch["messages"][0]["content"]
    assert "Cidade: Teresina-PI" in ficha and "BARBEARIA ZERO LTDA" in ficha and "aberto em 2015" in ficha
    assert "NÃO pergunte de novo" in ficha and "faturamento estimado" not in ficha.lower()
    tarefa = ch["messages"][-1]["content"]
    assert "cidade: Teresina" in tarefa and "segmento: Barbearia" in tarefa and "CNPJ aberto em 2015" in tarefa
    assert "Próximo assunto: marketing" in tarefa                    # nem situação nem cidade: a ficha já disse
    assert com.envios[-1]["texto"] == "Legal! Hoje como os clientes chegam até vocês?"
    c = conn.execute("select decisao, nota_detalhe from radar.conversas").fetchone()
    assert c[0] == "em_andamento" and c[1]["fatos"]["cidade"] == "Teresina" and c[1]["fatos"]["decisor"] == "dono_socio"
    assert conn.execute("select status from radar.leads").fetchone()[0] == "respondeu"


SEM_CIDADE = {k: v for k, v in TODOS_OK.items() if k not in ("cidade", "segmento")}


@pytest.mark.parametrize("horarios", [{"ok": False, "motivo": "sem_google"}, {"ok": False, "motivo": "sem_sdr"}])
async def test_radar_passa_direto_sem_google_passa_ao_sdr_sem_oferecer_horario(conn, horarios):
    from tests.test_conversa import lead_responde
    lead = preparar_radar_v2(conn)
    com = ComercialRadar(conn, horarios)
    await conversa.despachar(conn, com, AGORA)
    lead_responde(conn, lead, "Sou o dono, faturo 80 mil, quero começar já", AGORA + timedelta(minutes=2))
    # a IA não repete a cidade: o portão de cidade usa a do Maps (Teresina) e passa direto
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3), cliente_ia=FakeIA(resp(**SEM_CIDADE)))
    [pedido] = com.pedidos
    assert pedido["lead_id"] == lead and com.agendamentos == []
    assert com.envios[-1]["texto"] == triagem.MENSAGENS_PADRAO["lp"]["passar_sem_horario"]
    assert "—" not in com.envios[-1]["texto"] and "–" not in com.envios[-1]["texto"]
    [p] = com.passagens
    assert (p["lead_id"], p["motivo"], p["classificacao"], p["agendamento"]) == (lead, "qualificado", "passa_direto", None)
    assert "Cidade: Teresina" in p["resumo"] and "Empresa: Barbearia 0" in p["resumo"]
    assert f"horário não oferecido ({horarios['motivo']})" in p["resumo"]
    assert conn.execute("select status from radar.leads").fetchone()[0] == "qualificado"
    assert conn.execute("select estado, decisao from radar.conversas").fetchone() == ("humano", "passa_direto")


async def test_radar_com_google_oferece_os_2_horarios_do_comercial(conn):
    from tests.test_conversa import lead_responde
    lead = preparar_radar_v2(conn)
    com = ComercialRadar(conn, {"ok": True, "sdr_id": SDR, "opcoes": OPCOES})
    await conversa.despachar(conn, com, AGORA)
    lead_responde(conn, lead, "Tudo certo", AGORA + timedelta(minutes=2))
    await conversa.conversar(conn, com, AGORA + timedelta(minutes=3), cliente_ia=FakeIA(resp(**SEM_CIDADE)))
    assert OPCOES[0]["rotulo"] in com.envios[-1]["texto"] and OPCOES[1]["rotulo"] in com.envios[-1]["texto"]
    assert com.passagens == [] and conn.execute("select etapa from radar.conversas").fetchone()[0] == "oferta"


SABADO = datetime(2026, 10, 24, 13, 0, tzinfo=timezone.utc)                           # 10h em São Paulo


async def test_radar_v2_toque_1_so_em_dia_util(conn):
    preparar_radar_v2(conn)
    com = ComercialRadar(conn)
    assert await conversa.despachar(conn, com, SABADO) == 0                             # v2: só seg-sex 9h-18h
    assert await conversa.despachar(conn, com, SABADO + timedelta(days=2)) == 1         # segunda 10h


async def test_radar_fora_da_v2_quando_a_agencia_tira_volta_ao_fluxo_antigo(conn):
    from tests.test_conversa import lead_responde
    sabado = SABADO
    lead = preparar_radar_v2(conn)
    conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, 'triagem_regras', %s::jsonb)""",
                 (AGENCIA, json.dumps({"origens": ["lp"]})))
    com = ComercialRadar(conn)
    assert await conversa.despachar(conn, com, sabado) == 1
    lead_responde(conn, lead, "Oi", sabado + timedelta(minutes=2))
    from tests.test_conversa import resposta
    fake = FakeIA(resposta())
    await conversa.conversar(conn, com, sabado + timedelta(minutes=3), cliente_ia=fake)
    assert "seguindo um roteiro" not in fake.chamadas[0]["system"]
    assert "Próximo assunto" not in fake.chamadas[0]["messages"][-1]["content"]


async def test_radar_v2_pelo_openrouter_da_agencia(conn, monkeypatch):
    """O caminho da conversa do Radar na v2 chama o OpenRouter (provedor da config) com o esquema estrito v2."""
    from radar.chaves import cifrar
    from tests.test_conversa import lead_responde
    chave = "ab" * 32
    monkeypatch.setenv("ENCRYPTION_KEY", chave)
    lead = preparar_radar_v2(conn)
    conn.execute("""update public.agent_configs set radar_agent_provider = 'openrouter',
                        radar_agent_model = 'openai/gpt-5.6-luna' where agency_id = %s""", (AGENCIA,))
    conn.execute("insert into public.agent_provider_keys (agency_id, provider, api_key_encrypted) values (%s, 'openrouter', %s)",
                 (AGENCIA, cifrar("sk-or-teste", chave)))
    com = ComercialRadar(conn)
    await conversa.despachar(conn, com, AGORA)
    lead_responde(conn, lead, "Sou o dono", AGORA + timedelta(minutes=2))
    chamadas = []

    def handler(req):
        chamadas.append(json.loads(req.content))
        conteudo = resp("Hoje como os clientes chegam até vocês?", decisor="dono_socio").model_dump_json()
        return httpx.Response(200, json={"choices": [{"message": {"content": conteudo}}],
                                         "usage": {"prompt_tokens": 900, "completion_tokens": 90, "cost": 0.0003}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await conversa.conversar(conn, com, AGORA + timedelta(minutes=3), cliente_ia=http)
    [corpo] = chamadas
    assert corpo["model"] == "openai/gpt-5.6-luna"
    assert corpo["response_format"]["json_schema"]["schema"] == ia.ESQUEMA_RESPOSTA
    assert "Próximo assunto: marketing" in corpo["messages"][-1]["content"]
    assert com.envios[-1]["texto"] == "Hoje como os clientes chegam até vocês?"
    valor = conn.execute("select valor from radar.custos_api where fonte = 'ia_radar'").fetchone()[0]
    assert float(valor) == pytest.approx(0.0003 * ia.DOLAR)


# ── toque humano na triagem v2 (cliente HTTP real sobre o stub) ──────────
async def test_v2_reage_digita_e_envia_na_ordem_e_reage_na_escolha_de_horario(conn):
    cid, com = await lp_contatada(conn)
    assert [r for r, _ in com.chamadas] == ["enviar"]                                # 1ª mensagem: sem "digitando..."
    t = AGORA + timedelta(minutes=2)
    r = resp("E quem decide aí?", segmento="Clínica")
    r.reacao = "👍"
    await responde(conn, com, cid, "Clínica em Teresina", t, r)
    assert [r for r, _ in com.chamadas[1:]] == ["reagir", "digitando", "enviar"]
    ultima = conn.execute("""select id from public.messages where conversation_id = %s and sender_type = 'customer'
                             order by created_at desc limit 1""", (conv(conn, cid)["conversation_id"],)).fetchone()[0]
    [rg] = com.de("reagir")
    assert rg == {"agency_id": str(AGENCIA), "conversation_id": str(conv(conn, cid)["conversation_id"]),
                  "message_id": str(ultima), "emoji": "👍"}
    assert com.de("digitando")[0] == {"agency_id": str(AGENCIA),
                                      "conversation_id": str(conv(conn, cid)["conversation_id"])}

    await responde(conn, com, cid, "Sou a dona, faturo 80 mil, quero começar já", t + timedelta(minutes=5),
                   resp(**TODOS_OK))
    assert conv(conn, cid)["etapa"] == "oferta"
    escolha = resp("Ótimo!", escolha="opcao_1")
    escolha.reacao = "🙌"
    await responde(conn, com, cid, "Pode ser quarta", t + timedelta(minutes=8), escolha)
    assert [c["emoji"] for c in com.de("reagir")] == ["👍", "🙌"]


async def test_v2_pediu_humano_nao_reage(conn):
    cid, com = await lp_contatada(conn)
    r = resp("Claro, já chamo alguém do time.", intencao="pediu_humano")
    r.reacao = "👍"
    await responde(conn, com, cid, "Quero falar com uma pessoa", AGORA + timedelta(minutes=2), r)
    assert com.de("reagir") == [] and len(com.de("digitando")) == 1
