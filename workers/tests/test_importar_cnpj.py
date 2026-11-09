"""Parser dos arquivos da Receita (layout conferido em 2026-10) e importação contra Postgres real."""

import io
import zipfile

import pytest

from radar import importar_cnpj as ic
from tests.conftest import DSN


def z(texto: str, nome="ARQ.CSV") -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr(nome, texto.encode("latin-1"))
    return b.getvalue()


def picado(dados: bytes, n=7):
    return [dados[i:i + n] for i in range(0, len(dados), n)]


def estab(basico="12345678", ordem="0001", dv="90", fantasia="BARBEARIA DO ZÉ", sit="02", uf="PI", mun="1219", cnae="9602501"):
    c = [basico, ordem, dv, "1", fantasia, sit, "20200101", "00", "", "", "20190301", cnae, "", "RUA", "A", "100", "",
         "CENTRO", "64000000", uf, mun, "86", "99999999", "", "", "", "", "x@y.com", "", ""]
    return ";".join(f'"{x}"' for x in c)


def emp(basico="12345678", razao="JOSE SILVA 12345678901", porte="01"):
    return ";".join(f'"{x}"' for x in (basico, razao, "2135", "50", "1000,00", porte, ""))


MUNIS = "\n".join(f'"{i:04d}";"CIDADE {i}"' for i in range(5000, 10001)) + '\n"1219";"TERESINA"\n"6001";"PARATI"\n'


class OrigemFalsa:
    competencia = "teste"

    def __init__(self, est, emps):
        self.arq = {"Municipios.zip": z(MUNIS), "Estabelecimentos0.zip": z("\n".join(est) + "\n"),
                    "Empresas0.zip": z("\n".join(emps) + "\n")}
        for i in range(1, 10):
            self.arq[f"Estabelecimentos{i}.zip"] = z("")
            self.arq[f"Empresas{i}.zip"] = z("")

    def blocos(self, nome):
        return picado(self.arq[nome], 4096)


# ── fluxo e formato ──────────────────────────────────────────────────────
def test_inflar_em_pedacos_recompoe_o_arquivo():
    orig = "linha acentuada é ã\n" * 5000
    assert b"".join(ic.inflar_zip(picado(z(orig)))).decode("latin-1") == orig


def test_zip_truncado_aborta():
    dados = z("".join(f"{i}\n" for i in range(50000)))
    with pytest.raises(ic.ArquivoInvalido, match="truncado"):
        list(ic.inflar_zip([dados[:len(dados) // 2]]))


def test_crc_errado_aborta():
    dados = bytearray(z("abc" * 1000))
    dados[14] ^= 0xFF
    with pytest.raises(ic.ArquivoInvalido, match="CRC"):
        list(ic.inflar_zip([bytes(dados)]))


def test_linhas_atravessando_blocos_e_nul():
    assert list(ic.linhas([b'"a";"b\x00"\n"c', b'";"d"\n'])) == ['"a";"b"', '"c";"d"']


def test_aspas_soltas_nao_engolem_a_linha():
    assert ic.campos('"123";"NOME "COM" ASPAS";"X"') == ["123", 'NOME "COM" ASPAS', "X"]


def test_campos_vazios_conta_certa():
    assert len(ic.campos(estab())) == ic.COLUNAS_ESTAB


# ── LGPD ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("entrada,saida", [
    ("MARIA DA SILVA 12345678901", "MARIA DA SILVA"),
    ("MARIA DA SILVA 123.456.789-01", "MARIA DA SILVA"),
    ("12345678901 MARIA", "MARIA"),
    ("LOJA 12 DE OUTUBRO", "LOJA 12 DE OUTUBRO"),
    ("EMPRESA 12345678000190", "EMPRESA 12345678000190"),       # 14 dígitos não é CPF
])
def test_cpf_sai_do_nome(entrada, saida):
    assert ic.sem_cpf(entrada) == saida


def test_empresa_devolve_nome_sem_cpf_e_porte():
    assert ic.empresa(ic.campos(emp())) == ("12345678", "JOSE SILVA", "ME")


# ── regras de negócio ────────────────────────────────────────────────────
MUN = {"1219": "teresina"}


def test_so_ativa():
    for s in ("01", "03", "04", "08"):
        assert ic.estabelecimento(ic.campos(estab(sit=s)), MUN, None) is None
    assert ic.estabelecimento(ic.campos(estab()), MUN, None) == ("12345678000190", "12345678", "BARBEARIA DO ZÉ", "PI", "teresina", "9602501", "RUA A", "100", "64000000")


@pytest.mark.parametrize("cep,esperado", [("64000000", "64000000"), ("64.000-000", "64000000"), ("", None),
                                          ("123", None), ("00000000", None), ("123456789", None)])
def test_cep_oito_digitos_ou_nulo(cep, esperado):
    assert ic.normalizar_cep(cep) == esperado


def test_endereco_sem_cpf_e_sem_numero():
    c = ic.campos(estab())
    c[14], c[15] = "SITIO DE MARIA 12345678901", "S/N"
    r = ic.estabelecimento(c, MUN, None)
    assert r[6:8] == ("RUA SITIO DE MARIA", "S/N")


def test_filtro_de_uf_e_exterior():
    assert ic.estabelecimento(ic.campos(estab(uf="SP")), MUN, {"PI"}) is None
    assert ic.estabelecimento(ic.campos(estab(uf="EX")), MUN, None) is None


def test_cnpj_alfanumerico_aceito():
    r = ic.estabelecimento(ic.campos(estab(basico="12ABC34S", ordem="01DE", dv="35")), MUN, None)
    assert r[0] == "12ABC34S01DE35"


def test_municipio_desconhecido_e_linha_ruim():
    with pytest.raises(ValueError):
        ic.estabelecimento(ic.campos(estab(mun="9999")), MUN, None)


def test_alias_de_municipio_para_o_nome_do_ibge():
    assert ic.normalizar_municipio("PARATI") == "paraty"
    assert ic.normalizar_municipio("SÃO FELIPE D'OESTE") == "sao felipe d oeste"


def test_bitmap():
    b = ic.Bitmap()
    b.add("00000001"); b.add("99999999"); b.add("12ABC34S")
    assert "00000001" in b and "99999999" in b and "12ABC34S" in b
    assert "00000002" not in b and "12ABC34T" not in b


def test_layout_diferente_aborta():
    c = ic.Contagem(lidas=1000, ruins=2)
    with pytest.raises(ic.ArquivoInvalido, match="layout"):
        c.conferir("Estabelecimentos0")


# ── contra Postgres ──────────────────────────────────────────────────────
@pytest.fixture
def banco():
    if not DSN:
        pytest.skip("RADAR_TEST_DSN não definido")
    import psycopg
    c = psycopg.connect(DSN, autocommit=True)
    c.execute("truncate radar.cnpj_base")
    yield c
    c.execute("truncate radar.cnpj_base")
    c.close()


def linhas_base(c):
    return c.execute("select cnpj, razao_social, nome_fantasia, uf, municipio, cnae_principal, porte, nome_busca, "
                     "logradouro, numero, cep from radar.cnpj_base order by 1").fetchall()


def test_importa_cruza_empresa_e_filtra(banco):
    o = OrigemFalsa([estab(), estab(basico="22222222", sit="08"), estab(basico="33333333", uf="SP")],
                    [emp(), emp(basico="22222222"), emp(basico="33333333", razao="NAO ENTRA", porte="05")])
    r = ic.importar(banco, o, {"PI"})
    assert r["total"] == 1
    assert linhas_base(banco) == [("12345678000190", "JOSE SILVA", "BARBEARIA DO ZÉ", "PI", "teresina", "9602501", "ME",
                                   "barbearia ze jose silva", "RUA A", "100", "64000000")]
    assert "12345678901" not in str(linhas_base(banco))


def test_reimportar_e_idempotente_e_nao_regrava_linha_igual(banco):
    o = OrigemFalsa([estab()], [emp()])
    ic.importar(banco, o, {"PI"})
    xmin = banco.execute("select xmin::text from radar.cnpj_base").fetchone()[0]
    r = ic.importar(banco, o, {"PI"})
    assert (r["gravadas"], r["apagadas"]) == (0, 0)
    assert len(linhas_base(banco)) == 1
    assert banco.execute("select xmin::text from radar.cnpj_base").fetchone()[0] == xmin


def test_mudanca_so_de_endereco_atualiza_a_linha(banco):
    ic.importar(banco, OrigemFalsa([estab()], [emp()]), {"PI"})
    novo = estab().replace('"100"', '"200"', 1)
    r = ic.importar(banco, OrigemFalsa([novo], [emp()]), {"PI"})
    assert r["gravadas"] == 1 and linhas_base(banco)[0][9] == "200"


def test_baixada_no_mes_seguinte_sai_e_outro_estado_fica(banco):
    ic.importar(banco, OrigemFalsa([estab(), estab(basico="44444444"), estab(basico="55555555", uf="SP")],
                                   [emp(), emp(basico="44444444"), emp(basico="55555555")]))
    r = ic.importar(banco, OrigemFalsa([estab(), estab(basico="44444444", sit="08")], [emp(), emp(basico="44444444")]),
                    {"PI"})
    assert r["apagadas"] == 1
    assert [x[0] for x in linhas_base(banco)] == ["12345678000190", "55555555000190"]


def test_arquivo_truncado_nao_muda_nada(banco):
    ic.importar(banco, OrigemFalsa([estab()], [emp()]), {"PI"})
    antes = linhas_base(banco)
    o = OrigemFalsa([estab(basico="77777777")], [emp(basico="77777777")])
    o.arq["Empresas0.zip"] = o.arq["Empresas0.zip"][:40]
    with pytest.raises(ic.ArquivoInvalido):
        ic.importar(banco, o, {"PI"})
    assert linhas_base(banco) == antes


def test_carga_muito_menor_nao_apaga_a_base(banco):
    est = [estab(basico=f"{i:08d}") for i in range(10)]
    ic.importar(banco, OrigemFalsa(est, [emp(basico=f"{i:08d}") for i in range(10)]), {"PI"})
    with pytest.raises(ic.ArquivoInvalido, match="suspeita"):
        ic.importar(banco, OrigemFalsa(est[:2], [emp(basico=f"{i:08d}") for i in range(2)]), {"PI"})
    assert len(linhas_base(banco)) == 10


def test_recriar_indice_devolve_o_indice(banco):
    ic.importar(banco, OrigemFalsa([estab()], [emp()]), {"PI"}, recriar_indice=True)
    assert banco.execute("select 1 from pg_indexes where indexname = 'cnpj_base_nome_trgm_idx'").fetchone()
