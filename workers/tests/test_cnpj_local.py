"""Busca na base própria com Postgres real: acento, ruído do Maps, cidade homônima, decisão do cnpj_match."""

import pytest

from radar import cnpj_match
from radar.fontes.cnpj import completar_brasilapi
from radar.fontes.cnpj_local import BaseCnpjSemDados, CnpjBaseLocal, converter
from radar.importar_cnpj import nome_busca
from radar.worker import fonte_cnpj
from tests.conftest import DSN


def inserir(c, cnpj, razao, fantasia, uf, municipio, cnae, porte, logradouro="", numero="", cep=None):
    c.execute("""insert into radar.cnpj_base (cnpj, razao_social, nome_fantasia, uf, municipio, cnae_principal, porte,
                                              logradouro, numero, cep, nome_busca)
                 values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              (cnpj, razao, fantasia, uf, municipio, cnae, porte, logradouro, numero, cep, nome_busca(fantasia, razao)))


@pytest.fixture
def base():
    if not DSN:
        pytest.skip("RADAR_TEST_DSN não definido")
    import psycopg
    c = psycopg.connect(DSN, autocommit=True)
    c.execute("truncate radar.cnpj_base")
    linhas = [
        ("11111111000111", "JOSE S SILVA", "BARBEARIA DO ZÉ", "PI", "teresina", "9602501", "ME"),
        ("22222222000122", "PADARIA SÃO JOÃO LTDA", "", "PI", "teresina", "1091102", "EPP"),
        ("33333333000133", "BARBEARIA DO ZE LTDA", "", "MA", "teresina", "9602501", "ME"),     # outra UF
        ("44444444000144", "BARBEARIA DO ZE PARNAIBA", "", "PI", "parnaiba", "9602501", "ME"),  # outra cidade
        ("55555555000255", "CLINICA SORRISO S/A", "SORRISO", "PI", "teresina", "8630504", "DEMAIS"),
        ("66666666000166", "ACOUGUE BOI GORDO", "", "RJ", "paraty", "4722901", "ME"),
    ]
    for l in linhas:
        inserir(c, *l)
    yield c
    c.execute("truncate radar.cnpj_base")
    c.close()


async def cnpjs(c, nome, cidade, uf):
    return [d.cnpj for d in await CnpjBaseLocal(c).buscar_por_nome(nome, cidade, uf)]


async def test_nome_e_cidade_com_acento(base):
    assert (await cnpjs(base, "Barbearia do Zé", "Teresina", "PI"))[0] == "11111111000111"


async def test_acento_que_so_existe_na_base(base):
    assert (await cnpjs(base, "Padaria Sao Joao", "TERESINA", "pi")) == ["22222222000122"]


async def test_ruido_do_maps_no_nome(base):
    assert (await cnpjs(base, "BARBEARIA DO ZÉ - Corte & Barba | Centro", "Teresina", "PI"))[0] == "11111111000111"


async def test_erro_de_digitacao_ainda_acha(base):
    # nenhum termo é substring do nome na base: só a semelhança de trigramas acha
    assert "22222222000122" in await cnpjs(base, "Padariaa Joaoo", "Teresina", "PI")


async def test_cidade_homonima_nao_mistura_uf(base):
    assert "33333333000133" not in await cnpjs(base, "Barbearia do Zé", "Teresina", "PI")


async def test_outra_cidade_nao_entra(base):
    assert "44444444000144" not in await cnpjs(base, "Barbearia do Zé", "Teresina", "PI")


async def test_alias_do_nome_do_municipio(base):
    assert await cnpjs(base, "Açougue Boi Gordo", "Parati", "RJ") == ["66666666000166"]


async def test_custo_zero_e_nome_so_ruido(base):
    f = CnpjBaseLocal(base)
    assert await f.buscar_por_nome("Comércio de LTDA", "Teresina", "PI") == []
    assert f.custo_ultima_chamada == 0.0 and f.estimar(10) == 0.0


async def test_uf_nao_importada_falha_em_vez_de_dizer_nao_encontrado(base):
    with pytest.raises(BaseCnpjSemDados):
        await cnpjs(base, "Barbearia", "Palmas", "TO")


async def test_passa_pelo_cnpj_match(base):
    dados = await CnpjBaseLocal(base).buscar_por_nome("Barbearia do Zé", "Teresina", "PI")
    dec = cnpj_match.decidir(cnpj_match.Negocio("Barbearia do Zé", "R. A, 100", "Teresina"),
                             [cnpj_match.Candidato(d.cnpj, d.razao_social, d.nome_fantasia, d.logradouro, d.numero,
                                                   d.municipio, d.cep, "", d.situacao) for d in dados])
    assert dec.escolhido.candidato.cnpj == "11111111000111" and dec.escolhido.confianca == 60


async def decidir(c, nome, endereco, cep=""):
    dados = await CnpjBaseLocal(c).buscar_por_nome(nome, "Teresina", "PI")
    return cnpj_match.decidir(cnpj_match.Negocio(nome, endereco, "Teresina", cep),
                              [cnpj_match.Candidato(d.cnpj, d.razao_social, d.nome_fantasia, d.logradouro, d.numero,
                                                    d.municipio, d.cep, "", d.situacao) for d in dados])


async def test_endereco_chega_na_fonte(base):
    inserir(base, "70000000000170", "ESTETICA BELA LTDA", "ESTETICA BELA", "PI", "teresina", "9602502", "ME",
            "RUA DAS FLORES", "45", "64000111")
    d = (await CnpjBaseLocal(base).buscar_por_nome("Estética Bela", "Teresina", "PI"))[0]
    assert (d.logradouro, d.numero, d.cep) == ("RUA DAS FLORES", "45", "64000111")


async def test_nome_parecido_com_endereco_passa_do_corte(base):
    inserir(base, "70000000000170", "ESTETICA BELA VIDA LTDA", "ESTETICA BELA VIDA", "PI", "teresina", "9602502", "ME",
            "RUA DAS FLORES", "45", "64000111")
    nome = "Estética Bela Vidas Spa"
    assert cnpj_match._similar(cnpj_match.normalizar_nome(nome), "estetica bela vida") < 0.95
    dec = await decidir(base, nome, "R. das Flores, 45 - Centro, Teresina - PI", "64000-111")
    assert dec.escolhido.candidato.cnpj == "70000000000170"
    assert dec.escolhido.confianca == 80 and "nome parecido" in dec.escolhido.motivo and "endereço igual" in dec.escolhido.motivo


async def test_nome_parecido_sem_endereco_nao_passa(base):
    inserir(base, "70000000000170", "ESTETICA BELA VIDA LTDA", "ESTETICA BELA VIDA", "PI", "teresina", "9602502", "ME",
            "RUA DAS FLORES", "45", "64000111")
    dec = await decidir(base, "Estética Bela Vidas Spa", "Av. Outra, 900 - Centro, Teresina - PI")
    assert dec.escolhido is None and not dec.revisao_humana and dec.candidatos[0].confianca == 45


async def filiais(c):
    inserir(c, "80000000000180", "REDE SORRISO LTDA", "REDE SORRISO", "PI", "teresina", "8630504", "EPP", "RUA A", "100", "64000001")
    inserir(c, "80000000000280", "REDE SORRISO LTDA", "REDE SORRISO", "PI", "teresina", "8630504", "EPP", "AVENIDA FREI SERAFIM", "2000", "64000002")


async def test_filial_certa_pelo_endereco(base):
    await filiais(base)
    dec = await decidir(base, "Rede Sorriso", "Av. Frei Serafim, 2000 - Centro, Teresina - PI")
    assert dec.escolhido.candidato.cnpj == "80000000000280" and not dec.revisao_humana
    assert dec.escolhido.confianca == 90


async def test_filiais_de_mesmo_nome_sem_endereco_conhecido_vao_para_revisao(base):
    await filiais(base)
    dec = await decidir(base, "Rede Sorriso", "Rua Sem Relação, 5 - Centro, Teresina - PI")
    assert dec.escolhido is None and dec.revisao_humana


def test_converter_matriz_e_filial():
    d = converter(("55555555000255", "R", "F", "PI", "teresina", "8630504", "DEMAIS", "RUA A", "100", "64000000"))
    assert d.eh_filial and d.cnpj_matriz == "555555550001" and d.situacao == "ATIVA" and d.socios == []


def test_brasilapi_completa_o_que_a_base_nao_tem():
    d = converter(("11111111000111", "R", "F", "PI", "teresina", "9602501", "ME", "", "", None))
    completar_brasilapi(d, {"cnae_fiscal": 9602501, "data_inicio_atividade": "2019-03-01", "capital_social": 1000,
                            "natureza_juridica": "Empresário", "logradouro": "A", "numero": "100",
                            "descricao_tipo_de_logradouro": "RUA", "cep": "64000000", "opcao_pelo_simples": True,
                            "qsa": [{"nome_socio": "JOSE", "qualificacao_socio": "Titular"}]})
    assert (d.abertura.year, d.capital_social, d.logradouro, d.numero, d.cep, d.regime) == \
        (2019, 1000, "RUA A", "100", "64000000", "Simples")
    assert d.socios == [{"nome": "JOSE", "qualificacao": "Titular"}]


def test_escolha_da_fonte_por_env(monkeypatch):
    monkeypatch.delenv("RADAR_FONTE_CNPJ", raising=False)
    assert fonte_cnpj(None).chave == "base_local"
    monkeypatch.setenv("RADAR_FONTE_CNPJ", "casadosdados")
    assert fonte_cnpj(None).chave == "casadosdados"
    monkeypatch.setenv("RADAR_FONTE_CNPJ", "xpto")
    with pytest.raises(ValueError):
        fonte_cnpj(None)
