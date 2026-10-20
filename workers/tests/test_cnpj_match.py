from radar.cnpj_match import Candidato, Negocio, decidir, pontuar, texto_ficha

ZE = Negocio(nome="Barbearia do Zé", endereco="R. A, 100 - Centro", cidade="Teresina", cep="64000-000")


def cand(**kw):
    base = dict(cnpj="11111111000111", razao_social="JOSE S SILVA ME", nome_fantasia="BARBEARIA DO ZE",
                logradouro="RUA A", numero="100", municipio="TERESINA", cep="64000000", situacao="ATIVA")
    base.update(kw)
    return Candidato(**base)


def test_exemplo_do_levantamento_confianca_alta():
    r = pontuar(ZE, cand())
    assert r.confianca >= 90
    assert "nome fantasia igual" in r.motivo and "endereço igual" in r.motivo
    assert texto_ficha(r).startswith("CNPJ casado com confiança")


def test_cidade_diferente_zera():
    assert pontuar(ZE, cand(municipio="PARNAIBA")).confianca == 0


def test_so_cidade_nao_assume():
    d = decidir(ZE, [cand(nome_fantasia="PADARIA BOM PAO", razao_social="X LTDA", logradouro="AV Y", numero="9")])
    assert d.escolhido is None and not d.revisao_humana
    assert d.candidatos[0].confianca < 50


def test_dois_candidatos_parecidos_vai_para_revisao():
    d = decidir(ZE, [cand(), cand(cnpj="22222222000122")])
    assert d.escolhido is None and d.revisao_humana
    assert len(d.candidatos) == 2


def test_candidato_claro_vence():
    d = decidir(ZE, [cand(), cand(cnpj="2", nome_fantasia="BARBEARIA CENTRAL", logradouro="RUA B", numero="5")])
    assert d.escolhido.candidato.cnpj == "11111111000111"


def test_cnpj_baixado_perde_pontos():
    assert pontuar(ZE, cand(situacao="BAIXADA")).confianca < pontuar(ZE, cand()).confianca
