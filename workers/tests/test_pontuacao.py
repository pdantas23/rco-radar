import pytest

from radar.pontuacao import PESOS_PADRAO, LeadParaPontuar, Regra, escolher_regra, pontuar


def ze(**kw):
    base = dict(anuncio_ativo="nao", presenca_digital="media", tem_whatsapp_provavel=True, whatsapp=None,
                instagram_user="barbeariadoze", telefone="+5586999998888",
                faturamento_estimado_faixa="30k_80k", nota=4.6, qtd_avaliacoes=180, cnpj_situacao="ATIVA")
    base.update(kw)
    return LeadParaPontuar(**base)


def test_exemplo_do_levantamento_com_pesos_da_v1():
    p = pontuar(ze(), Regra(versao=1))
    assert p.total == 100 and p.classificacao == "quente"
    assert all(c.bateu for c in p.criterios if not c.nome.startswith("zera_"))
    # anúncios ficaram para o futuro: o critério nem aparece na conta aberta
    assert "sem_anuncio_ativo" not in {c.nome for c in p.criterios}


def test_criterio_de_anuncio_volta_quando_admin_inclui_o_peso():
    regra = Regra(versao=2, pesos={**PESOS_PADRAO, "sem_anuncio_ativo": 25})
    assert pontuar(ze(), regra).total == 100                         # teto 100
    p = pontuar(ze(anuncio_ativo="nao_verificado"), regra)
    assert {c.nome: c.pontos for c in p.criterios}["sem_anuncio_ativo"] == 0


def test_peso_removido_pelo_admin_nao_conta():
    regra = Regra(versao=3, pesos={k: v for k, v in PESOS_PADRAO.items() if k != "cnpj_ativo"})
    p = pontuar(ze(), regra)
    assert p.total == 85 and "cnpj_ativo" not in {c.nome for c in p.criterios}


@pytest.mark.parametrize("kw", [
    dict(cnpj_situacao="BAIXADA"),
    dict(fechado_permanentemente=True),
    dict(tem_whatsapp_provavel=False, instagram_user=None, telefone=None),
])
def test_zera(kw):
    p = pontuar(ze(**kw), Regra(versao=1))
    assert p.total == 0 and p.classificacao == "frio"


def test_faixas_editaveis():
    p = pontuar(ze(cnpj_situacao=None), Regra(versao=2, faixas={"quente": 90, "morno": 40}))   # 85 pts
    assert p.classificacao == "morno" and p.versao_regra == 2


def test_regra_do_nicho_vence_a_geral():
    geral = Regra(versao=3)
    barb = Regra(versao=2, nicho="barbearia", pesos={"sem_anuncio_ativo": 30})
    assert escolher_regra([geral, barb], "Barbearia") is barb
    assert escolher_regra([geral, barb], "farmácia") is geral
