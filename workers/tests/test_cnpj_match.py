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


# Casos reais da rodada de barbearias em Teresina (17/11/2026, base própria da Receita).

def _neg(nome, endereco, cep):
    return Negocio(nome=nome, endereco=f"{nome} - {endereco}, Teresina - PI, {cep}", cidade="Teresina", cep=cep)


def test_nome_igual_em_outro_endereco_nao_assume():
    """Maverick barbearia × MAVERICK restaurante do outro lado da cidade."""
    neg = _neg("Maverick", "Alameda Parnaíba, 1783 - Vila Operaria", "64002-475")
    d = decidir(neg, [cand(nome_fantasia="MAVERICK", razao_social="ISAAC MESQUITA RESTAURANTE LTDA",
                           logradouro="AVENIDA UNIVERSITARIA", numero="880", cep="64049550")])
    assert d.escolhido is None and d.revisao_humana       # nome idêntico: um humano olha, não some
    assert "endereço não confere" in d.candidatos[0].motivo


def test_outra_barbearia_no_mesmo_cep_nao_assume():
    """LB Barbearia × Barbearia H2I: mesma avenida e CEP, nome diferente."""
    neg = _neg("LB BARBEARIA THE", "Av. Noronha Almeida, 2543 - 1 - São João", "64045-500")
    d = decidir(neg, [cand(nome_fantasia="BARBEARIA H2I", razao_social="H2I BARBEARIA LTDA",
                           logradouro="AVENIDA NORONHA ALMEIDA", numero="2400", cep="64045500")])
    assert d.escolhido is None and not d.revisao_humana   # nome diferente: não encontrado, sem revisão
    assert "nome não confere" in d.candidatos[0].motivo


def test_palavra_do_ramo_nao_conta_como_nome_parecido():
    neg = _neg("Business Barbearia", "Av. Homero Castelo Branco, 615 - Jóquei", "64048-400")
    r = pontuar(neg, cand(nome_fantasia="BERG BARBEARIA", razao_social="JOSEMBERG BARBEARIA LTDA",
                          logradouro="AVENIDA HOMERO CASTELO BRANCO", numero="700", cep="64048400"))
    assert "nome parecido" not in r.motivo and r.confianca < 50


def test_endereco_do_maps_com_nome_na_frente_casa_a_rua():
    """Relax Barbearia Teresina × RELAX BARBER SHOP no mesmo número: antes ficava 'nao_encontrado'."""
    neg = _neg("Relax Barbearia Teresina", "Av. Raul Lopes, 880 - Lj 02 - Jóquei", "64048-065")
    d = decidir(neg, [cand(nome_fantasia="RELAX BARBER SHOP", razao_social="EMPREENDIMENTOS BARBERSHOP RELAX LTDA",
                           logradouro="AVENIDA RAUL LOPES", numero="880", cep="64048065")])
    assert d.escolhido is not None
    assert "endereço igual" in d.escolhido.motivo


def test_filial_certa_vence_pelo_endereco():
    """Bunker Matriz (Av. Rio Poti, 2656) × duas Bunker: a do mesmo número ganha, a outra não chega ao limiar."""
    neg = _neg("Bunker Barbearia Matriz", "Av. Rio Poti, 2656 - Fátima", "64052-790")
    certa = cand(cnpj="1", nome_fantasia="BUNKER BARBEARIA AV RIO POTI", razao_social="BUNKER BARBEARIA RIO POTI LTDA",
                 logradouro="AVENIDA POTI", numero="2656", cep="64051210")
    outra = cand(cnpj="2", nome_fantasia="BUNKER BARBEARIA", razao_social="BUNKER BARBEARIA LTDA",
                 logradouro="RUA QUALQUER", numero="10", cep="64000111")
    d = decidir(neg, [outra, certa])
    assert d.escolhido is not None and d.escolhido.candidato.cnpj == "1"


def test_descricao_depois_da_barra_e_ignorada():
    neg = Negocio(nome="Barba Negra | Barbearia em Teresina",
                  endereco="Barba Negra | Barbearia em Teresina - R. Valdemar Martins, 3730 - Morada do Sol, "
                           "Teresina - PI, 64056-130", cidade="Teresina", cep="64056-130")
    d = decidir(neg, [cand(nome_fantasia="BARBEARIA BARBA NEGRA", razao_social="BARBEARIA BARBA NEGRA LTDA",
                           logradouro="RUA VALDEMAR MARTINS", numero="1", cep="64055280")])
    assert d.escolhido is not None and "mesma rua" in d.escolhido.motivo


def test_telefone_igual_dispensa_endereco():
    neg = Negocio(nome="Barbearia do Zé", endereco="", cidade="Teresina", telefone="+5586999990000")
    r = pontuar(neg, cand(logradouro="RUA Z", numero="9", cep="", telefone="+5586999990000"))
    assert r.confianca >= 50


# Rodada real de lojas de celular em Teresina (17/11/2026): a lista fixa só cobria barbearia.
RAMO = "Loja de celular Loja de celulares"


def test_nicho_da_pesquisa_nao_conta_como_nome():
    """VM CELL casava com DMCELL só pela rua: 'cell' é palavra do ramo."""
    neg = Negocio(nome="VM CELL - Assistência Técnica de Celular Teresina", endereco="Rua Coelho Rodrigues, 1500 - Centro",
                  cidade="Teresina", ramo=RAMO)
    d = decidir(neg, [cand(nome_fantasia="DMCELL", razao_social="DMCELL COMERCIO E SERVICOS LTDA",
                           logradouro="RUA COELHO RODRIGUES", numero="1720", cep="64000999")])
    assert d.escolhido is None and not d.revisao_humana


def test_nome_so_com_palavra_do_ramo_nao_casa_com_ninguem():
    neg = Negocio(nome="Celular", endereco="Av. Frei Serafim, 10", cidade="Teresina", ramo=RAMO)
    d = decidir(neg, [cand(nome_fantasia="THE CELULAR", razao_social="THE CELULAR LTDA", logradouro="RUA X", numero="1"),
                      cand(cnpj="2", nome_fantasia="CASA DO CELULAR", razao_social="CDC LTDA", logradouro="RUA Y", numero="2")])
    assert d.escolhido is None and not d.revisao_humana
    assert all(r.confianca < 50 for r in d.candidatos)


def test_ramo_nao_atrapalha_o_casamento_certo():
    neg = Negocio(nome="T&T Cell", endereco="T&T Cell - Rua Areolino de Abreu, 1200 - Centro", cidade="Teresina",
                  cep="64000-180", ramo=RAMO)
    d = decidir(neg, [cand(nome_fantasia="T & T CELL", razao_social="A C OLIVEIRA LIMA LTDA",
                           logradouro="RUA AREOLINO DE ABREU", numero="1200", cep="64000180")])
    assert d.escolhido is not None and d.escolhido.confianca >= 90


def test_nome_contido_com_so_a_mesma_rua_nao_assume():
    """Dirceu Celular × DIRCEU VET (veterinário) na mesma rua, outro número."""
    neg = Negocio(nome="Dirceu Celular", endereco="Rua Paissandu, 1200 - Centro", cidade="Teresina", ramo=RAMO)
    d = decidir(neg, [cand(nome_fantasia="DIRCEU VET", razao_social="D S VETERINARIA LTDA",
                           logradouro="RUA PAISSANDU", numero="900", cep="64000555")])
    assert d.escolhido is None


def test_nome_so_do_ramo_identico_e_com_endereco_casa():
    """Shop do Celular = SHOP DO CELULAR, mesmo endereço e CEP: era acerto e não pode se perder."""
    neg = Negocio(nome="Shop do Celular", endereco="Shop do Celular - Rua Simplício Mendes, 300 - Centro",
                  cidade="Teresina", cep="64000-110", ramo=RAMO)
    d = decidir(neg, [cand(nome_fantasia="SHOP DO CELULAR", razao_social="F R ALVES SIQUEIRA LTDA",
                           logradouro="RUA SIMPLICIO MENDES", numero="300", cep="64000110")])
    assert d.escolhido is not None and d.escolhido.confianca >= 90


def test_nome_contido_com_numero_igual_casa():
    neg = Negocio(nome="PHONE STORE TERESINA RIO POTY", endereco="Av. Marechal Castelo Branco, 911",
                  cidade="Teresina", cep="64000-200", ramo=RAMO)
    d = decidir(neg, [cand(nome_fantasia="PHONE STORE TERESINA", razao_social="PHONE STORE COMERCIO LTDA",
                           logradouro="AVENIDA MARECHAL CASTELO BRANCO", numero="911", cep="64000200")])
    assert d.escolhido is not None


def test_nome_do_shopping_no_nome_da_loja_nao_identifica():
    """PHONE STORE TERESINA RIO POTY empatava com BAGAGGIO TERESINA RIO POTY no mesmo shopping."""
    neg = Negocio(nome="PHONE STORE TERESINA RIO POTY",
                  endereco="PHONE STORE TERESINA RIO POTY - Av. Mar. Castelo Branco, 911 - Piso L3 do SHOPPING - RIO POTY, "
                           "Teresina - PI, 64003-901", cidade="Teresina", cep="64003-901", ramo=RAMO)
    certa = cand(cnpj="1", nome_fantasia="PHONE STORE TERESINA", razao_social="PHONE STORE COMERCIO LTDA",
                 logradouro="AVENIDA MARECHAL CASTELO BRANCO", numero="911", cep="64003901")
    vizinha = cand(cnpj="2", nome_fantasia="", razao_social="BAGAGGIO TERESINA RIO POTY LTDA",
                   logradouro="AVENIDA MARECHAL CASTELO BRANCO", numero="911", cep="64003901")
    d = decidir(neg, [vizinha, certa])
    assert d.escolhido is not None and d.escolhido.candidato.cnpj == "1"


def test_nome_repetido_antes_da_rua_no_endereco_continua_sendo_nome():
    neg = Negocio(nome="ItemTudo Cell", endereco="ItemTudo Cell - Loja Itemtudo Cell Av. Pres. Kennedy, 1585 Loja E 10 - "
                  "Horto Florestal, Teresina - PI, 64052-675", cidade="Teresina", cep="64052-675", ramo=RAMO)
    d = decidir(neg, [cand(nome_fantasia="ITEMTUDO CEL", razao_social="A U CAMARA",
                           logradouro="AVENIDA PRESIDENTE KENNEDY", numero="1585", cep="64052675")])
    assert d.escolhido is not None
