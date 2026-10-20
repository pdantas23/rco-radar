from datetime import datetime, time, timedelta

import pytest

from radar.gate import Contexto, checar, pediu_para_parar

AGORA = datetime(2026, 10, 19, 10, 0)
BOA = ("Oi, falo com o responsável pela Barbearia do Zé? Vi que vocês têm nota 4,6 no Google. "
       "Se não quiser receber mensagens, responda SAIR.")


def ctx(**kw):
    base = dict(lead_status="aprovado_para_contato", motivo_descarte=None, bloqueado=False,
                ultimo_contato_mesmo_canal=None, confirmado_recontato=False, disparo_confirmado=True,
                registrado_em_disparos_leads=True, agora_local_lead=AGORA, janela_inicio=time(8),
                janela_fim=time(20), canal="whatsapp_nao_oficial", template_aprovado=False,
                primeira_mensagem=True)
    base.update(kw)
    return Contexto(**base)


def test_mensagem_boa_passa():
    assert checar(BOA, ctx())


@pytest.mark.parametrize("kw,trecho", [
    (dict(disparo_confirmado=False), "confirmação humana"),
    (dict(registrado_em_disparos_leads=False), "registro prévio"),
    (dict(bloqueado=True), "pediu para não receber"),
    (dict(lead_status="descartado", motivo_descarte="pediu_para_nao_receber"), "pediu para não receber"),
    (dict(ultimo_contato_mesmo_canal=AGORA - timedelta(days=10)), "menos de 30 dias"),
    (dict(agora_local_lead=AGORA.replace(hour=21)), "fora da janela"),
    (dict(agora_local_lead=AGORA.replace(hour=7, minute=59)), "fora da janela"),
    (dict(canal="whatsapp_oficial"), "template aprovado"),
    (dict(tem_midia=True), "mídia na primeira"),
])
def test_bloqueia(kw, trecho):
    r = checar(BOA, ctx(**kw))
    assert not r and any(trecho in m for m in r.motivos), r.motivos


def test_recontato_com_confirmacao_extra_passa():
    assert checar(BOA, ctx(ultimo_contato_mesmo_canal=AGORA - timedelta(days=10), confirmado_recontato=True))


@pytest.mark.parametrize("texto,trecho", [
    (BOA + " Veja https://rcohub.com.br", "link"),
    (BOA.replace("Barbearia do Zé", "{nome_negocio}"), "variável"),
    (BOA + " Resultado garantido.", "proibida"),
    ("Oi, tudo bem? Posso falar com o responsável?", "opt-out"),
    ("", "vazia"),
])
def test_conteudo(texto, trecho):
    r = checar(texto, ctx())
    assert not r and any(trecho in m for m in r.motivos), r.motivos


def test_link_permitido_depois_da_primeira():
    assert checar("Segue o site: https://rcohub.com.br", ctx(primeira_mensagem=False))


@pytest.mark.parametrize("texto,esperado", [
    ("SAIR", True), ("sair.", True), ("Não quero receber isso", True), ("pare", True),
    ("quero sair da fila de terça", False), ("me fala mais", False),
])
def test_opt_out(texto, esperado):
    assert pediu_para_parar(texto) is esperado
