"""Toque humano: "digitando..." com duração pelo tamanho do texto e reação filtrada (lista fechada, intenção)."""

import json

import httpx
import pytest
import respx

from radar import ia, toque_humano
from radar.comercial import Comercial

AG = "aaaaaaaa-1111-0000-0000-000000000001"
CONV = "dddddddd-0000-0000-0000-000000000001"


@pytest.fixture
def esperas(sem_espera_de_digitando):
    return sem_espera_de_digitando          # conftest: a espera é registrada, não feita


class FakeComercial:
    def __init__(self, digitando=None, reagir=None, erro=None):
        self.resp_digitando = digitando or {"ok": True}
        self.resp_reagir = reagir or {"ok": True}
        self.erro = erro
        self.digitandos, self.reacoes = [], []

    async def digitando(self, **kw):
        self.digitandos.append(kw)
        if self.erro:
            raise self.erro
        return self.resp_digitando

    async def reagir(self, **kw):
        self.reacoes.append(kw)
        if self.erro:
            raise self.erro
        return self.resp_reagir


# ── duração ──────────────────────────────────────────────────────────────
def test_duracao_cresce_com_o_texto_e_tem_teto():
    assert toque_humano.duracao_digitando("") == 1.5
    assert toque_humano.duracao_digitando("Oi!") == pytest.approx(1.5 + 3 * 0.035)
    assert toque_humano.duracao_digitando("  Oi!  ") == toque_humano.duracao_digitando("Oi!")
    assert toque_humano.duracao_digitando("x" * 100) == pytest.approx(5.0)
    assert toque_humano.duracao_digitando("x" * 1000) == 8.0
    assert toque_humano.duracao_digitando(None) == 1.5


async def test_digitar_espera_o_tempo_e_renova_o_indicador(esperas):
    com = FakeComercial()
    total = await toque_humano.digitar(com, agency_id=AG, conversation_id=CONV, texto="x" * 1000)
    assert total == 8.0
    assert esperas == [5.0, 3.0]                       # renova a cada 5 s
    assert len(com.digitandos) == 2 and com.digitandos[0] == {"agency_id": AG, "conversation_id": CONV}


async def test_digitar_mensagem_curta_um_disparo_so(esperas):
    com = FakeComercial()
    assert await toque_humano.digitar(com, agency_id=AG, conversation_id=CONV, texto="Show! E a cidade?") > 1.5
    assert len(com.digitandos) == 1 and len(esperas) == 1


@pytest.mark.parametrize("com", [FakeComercial(erro=RuntimeError("COMERCIAL /digitando: HTTP 502")),
                                 FakeComercial(digitando={"ok": False, "motivo": "conexao_nao_pertence"})])
async def test_digitar_falha_nao_segura_nem_espera(esperas, com):
    assert await toque_humano.digitar(com, agency_id=AG, conversation_id=CONV, texto="Oi") == 0.0
    assert esperas == []


async def test_digitar_sem_rota_no_cliente_nao_quebra(esperas):
    assert await toque_humano.digitar(object(), agency_id=AG, conversation_id=CONV, texto="Oi") == 0.0


def test_espera_padrao_e_assincrona():
    import asyncio
    import importlib
    assert importlib.reload(toque_humano).dormir is asyncio.sleep           # nunca time.sleep (travaria o laço)


# ── reação ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("emoji", ["👍", "😊", "🙌", "👏", "❤️", " 👍 "])
def test_filtro_deixa_a_lista_fechada(emoji):
    assert toque_humano.filtrar_reacao(emoji, "continuar") == emoji.strip()


def test_filtro_normaliza_coracao_sem_seletor():
    assert toque_humano.filtrar_reacao("❤", "qualificado") == "❤️"


@pytest.mark.parametrize("emoji", ["", None, "😂", "👎", "🔥", "👍👍", "👍🏽", "ok", "👍 Show"])
def test_filtro_descarta_fora_da_lista(emoji):
    assert toque_humano.filtrar_reacao(emoji, "continuar") is None


@pytest.mark.parametrize("intencao", ["pediu_humano", "sem_interesse", "nao_sei"])
def test_filtro_descarta_em_intencao_que_nao_e_positiva(intencao):
    assert toque_humano.filtrar_reacao("👍", intencao) is None


async def test_reage_a_ultima_mensagem_do_lead():
    com = FakeComercial()
    msgs = [{"id": "m1", "sender_type": "customer"}, {"id": "m2", "sender_type": "customer"}]
    assert await toque_humano.reagir(com, agency_id=AG, conversation_id=CONV, mensagens_do_lead=msgs,
                                     reacao="🙌", intencao="continuar") == "🙌"
    assert com.reacoes == [{"agency_id": AG, "conversation_id": CONV, "message_id": "m2", "emoji": "🙌"}]


async def test_sem_id_na_ultima_mensagem_nao_reage():
    com = FakeComercial()
    msgs = [{"id": "m1"}, {"id": None}]
    assert await toque_humano.reagir(com, agency_id=AG, conversation_id=CONV, mensagens_do_lead=msgs,
                                     reacao="👍", intencao="continuar") is None
    assert com.reacoes == []


async def test_emoji_fora_da_lista_nem_chama_o_comercial():
    com = FakeComercial()
    assert await toque_humano.reagir(com, agency_id=AG, conversation_id=CONV, mensagens_do_lead=[{"id": "m1"}],
                                     reacao="😂", intencao="continuar") is None
    assert await toque_humano.reagir(com, agency_id=AG, conversation_id=CONV, mensagens_do_lead=[],
                                     reacao="👍", intencao="continuar") is None
    assert com.reacoes == []


@pytest.mark.parametrize("com", [FakeComercial(reagir={"ok": True, "pulado": "limite_reacoes"}),
                                 FakeComercial(reagir={"ok": False, "motivo": "conexao_offline"}),
                                 FakeComercial(erro=RuntimeError("HTTP 502"))])
async def test_ritmo_ou_falha_do_comercial_nao_quebra(com):
    assert await toque_humano.reagir(com, agency_id=AG, conversation_id=CONV, mensagens_do_lead=[{"id": "m1"}],
                                     reacao="👍", intencao="continuar") is None


# ── cliente HTTP e IA ────────────────────────────────────────────────────
@respx.mock
async def test_cliente_chama_as_rotas_com_o_segredo():
    d = respx.post("http://c/api/internal/radar/digitando").mock(return_value=httpx.Response(200, json={"ok": True}))
    r = respx.post("http://c/api/internal/radar/reagir").mock(return_value=httpx.Response(200, json={"ok": True}))
    com = Comercial("http://c", "seg")
    assert await com.digitando(agency_id=AG, conversation_id=CONV) == {"ok": True}
    assert await com.reagir(agency_id=AG, conversation_id=CONV, message_id="m1", emoji="👍") == {"ok": True}
    assert d.calls[0].request.headers["x-radar-secret"] == "seg"
    assert json.loads(d.calls[0].request.content) == {"agency_id": AG, "conversation_id": CONV}
    assert json.loads(r.calls[0].request.content) == {"agency_id": AG, "conversation_id": CONV, "message_id": "m1",
                                                      "emoji": "👍"}


def test_resposta_sem_reacao_continua_valida_e_prompt_orienta():
    r = ia.RespostaIA.model_validate({"mensagem": "Oi", "campos": {}, "intencao": "continuar", "resumo": "x"})
    assert r.reacao == ""
    s = ia.montar_sistema("RCO HUB", None)
    assert "reacao" in s and "👍 😊 🙌 👏 ❤️" in s and "uma reação a cada quatro respostas" in s
    assert "—" not in ia.REGRA_REACAO and "–" not in ia.REGRA_REACAO
