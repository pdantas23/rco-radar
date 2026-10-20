import pytest

from radar.pontuacao import LeadParaPontuar, Regra, escolher_regra, pontuar


def ze(**kw):
    base = dict(anuncio_ativo="nao", presenca_digital="media", tem_whatsapp_provavel=True, whatsapp=None,
                instagram_user="barbeariadoze", telefone="+5586999998888",
                faturamento_estimado_faixa="30k_80k", nota=4.6, qtd_avaliacoes=180, cnpj_situacao="ATIVA")
    base.update(kw)
    return LeadParaPontuar(**base)


def test_exemplo_do_levantamento_95_quente():
    p = pontuar(ze(), Regra(versao=1))
    assert p.total == 95 and p.classificacao == "quente"
    assert all(c.bateu for c in p.criterios if not c.nome.startswith("zera_"))


def test_anuncio_nao_verificado_nao_ganha_ponto():
    p = pontuar(ze(anuncio_ativo="nao_verificado"), Regra(versao=1))
    assert p.total == 70


@pytest.mark.parametrize("kw", [
    dict(cnpj_situacao="BAIXADA"),
    dict(fechado_permanentemente=True),
    dict(tem_whatsapp_provavel=False, instagram_user=None, telefone=None),
])
def test_zera(kw):
    p = pontuar(ze(**kw), Regra(versao=1))
    assert p.total == 0 and p.classificacao == "frio"


def test_faixas_editaveis():
    p = pontuar(ze(), Regra(versao=2, faixas={"quente": 99, "morno": 40}))
    assert p.classificacao == "morno" and p.versao_regra == 2


def test_regra_do_nicho_vence_a_geral():
    geral = Regra(versao=3)
    barb = Regra(versao=2, nicho="barbearia", pesos={"sem_anuncio_ativo": 30})
    assert escolher_regra([geral, barb], "Barbearia") is barb
    assert escolher_regra([geral, barb], "farmácia") is geral
