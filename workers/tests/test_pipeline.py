"""Pipeline completo (minerar -> enriquecer -> pontuar) contra Postgres real, com fontes falsas.

Usa RADAR_TEST_DSN (banco descartável com a migration 001). Sem a variável, pula.
"""

import os
from datetime import date, datetime, timezone

import psycopg
import pytest

from radar import etapas, fila
from radar.fontes.base import Avaliacao, DadosCnpj, NegocioMaps, PerfilInstagram, ResultadoAnuncios
from radar.site import ResultadoSite
from radar.worker import processar_um

DSN = os.environ.get("RADAR_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="RADAR_TEST_DSN não definido")
USUARIO = "00000000-0000-0000-0000-0000000000aa"


def negocio(pid, nome, **kw):
    base = dict(place_id=pid, nome=nome, categoria="Barbearia", endereco="R. A, 100 - Centro, Teresina - PI",
                cidade="Teresina", estado="PI", cep="64000-000", lat=-5.09, lng=-42.80,
                telefone="(86) 99999-8888", site=None, nota=4.6, qtd_avaliacoes=180, horario=None,
                link_maps=f"https://maps.google.com/?cid={pid}", fechado_permanentemente=False,
                avaliacoes=[Avaliacao("", 5, f"ótimo {i}", datetime(2026, 10, i + 1, tzinfo=timezone.utc)) for i in range(7)])
    base.update(kw)
    return NegocioMaps(**base)


class FakeMaps:
    chave, custo_ultima_chamada = "fake_maps", 0.0

    def __init__(self, negocios):
        self.negocios = negocios

    async def buscar(self, consulta, uf_padrao=""):
        self.custo_ultima_chamada = 0.03 * len(self.negocios)
        return list(self.negocios)


class FakeCnpj:
    chave, custo_ultima_chamada = "fake_cnpj", 0.0

    async def buscar_por_nome(self, nome, municipio, uf):
        self.custo_ultima_chamada = 0.01
        if "Zé" not in nome:
            return []
        return [DadosCnpj(cnpj="11111111000111", razao_social="JOSE S SILVA ME", nome_fantasia="BARBEARIA DO ZE",
                          situacao="ATIVA", abertura=date(2019, 3, 1), porte="ME", natureza="Empresário",
                          capital_social=10000, cnae_principal="9602501", cnae_descricao="Cabeleireiros",
                          cnaes_secundarios=[], regime="Simples", socios=[{"nome": "JOSE", "qualificacao": "Titular"}],
                          email_rf=None, telefone_rf=None, logradouro="RUA A", numero="100", municipio="TERESINA",
                          uf="PI", cep="64000000", eh_filial=False, cnpj_matriz="111111110001")]


class FakeInstagram:
    chave, custo_ultima_chamada = "fake_ig", 0.0

    def __init__(self, falhar=False):
        self.falhar = falhar

    async def procurar(self, nome, cidade):
        if self.falhar:
            raise RuntimeError("bloqueado pelo Instagram")
        return ["barbeariadoze"] if "Zé" in nome else []

    async def perfil(self, usuario):
        self.custo_ultima_chamada = 0.01
        return PerfilInstagram(usuario, 2300, 300, 400, datetime(2026, 10, 15, tzinfo=timezone.utc), 9,
                               "Agende pelo WhatsApp", True, True, True)


class FakeAnuncios:
    chave, custo_ultima_chamada = "fake_ads", 0.0

    async def verificar(self, pagina, nome, pais="BR"):
        self.custo_ultima_chamada = 0.004
        return ResultadoAnuncios(None, pagina, 0, None, None, [])


async def site_sem_site(url):
    return ResultadoSite(site_existe=False)


async def geo(cidade, uf):
    return (-5.09, -42.80)


@pytest.fixture
def conn():
    c = psycopg.connect(DSN, autocommit=True)
    c.execute("""truncate radar.jobs, radar.pesquisas_leads, radar.avaliacoes, radar.empresas, radar.presenca,
                 radar.anuncios, radar.pontuacoes, radar.regras_pontuacao, radar.custos_api, radar.historico,
                 radar.pesquisas, radar.leads, radar.config cascade""")
    c.execute("insert into auth.users values (%s) on conflict do nothing", (USUARIO,))
    c.execute("insert into radar.usuarios (id, nome) values (%s, 'teste') on conflict do nothing", (USUARIO,))
    yield c
    c.close()


def criar_pesquisa(conn, **filtros) -> str:
    pid = conn.execute(
        """insert into radar.pesquisas (nome, nicho_texto, lugar_estado, lugar_cidade, raio_km, filtros,
                 fonte_maps, usuario_id, status) values ('barbearia · Teresina', 'barbearia', 'PI', 'Teresina',
                 15, %s, 'fake_maps', %s, 'na_fila') returning id""",
        (psycopg.types.json.Jsonb(filtros), USUARIO)).fetchone()[0]
    fila.enfileirar(conn, "minerar", pesquisa_id=pid)
    return pid


async def rodar_tudo(conn, f, limite=200):
    for _ in range(limite):
        if not await processar_um(conn, f, "teste"):
            return
        conn.execute("update radar.jobs set agendado_para = now() where status = 'pendente'")  # pula backoff
    raise AssertionError("fila não esvaziou")


def fontes(negocios, ig=None):
    return etapas.Fontes(FakeMaps(negocios), FakeCnpj(), ig or FakeInstagram(), FakeAnuncios(), geo, site_sem_site)


async def test_exemplo_do_levantamento_vira_95_quente(conn):
    pid = criar_pesquisa(conn, max_resultados=200, nota_minima=4.0)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    lead = conn.execute("""select status, pontuacao, classificacao, anuncio_ativo, presenca_digital,
                                  faturamento_estimado_faixa, cnpj_status, tem_whatsapp_provavel, telefone
                           from radar.leads""").fetchone()
    # porte ME + 180 avaliações + ritmo => 30–80 mil, como no exemplo do levantamento
    assert lead == ("pontuado", 95, "quente", "nao", "media", "30k_80k", "encontrado", True, "+5586999998888")
    assert conn.execute("select count(*) from radar.avaliacoes").fetchone()[0] == 5
    assert conn.execute("select status from radar.pesquisas where id = %s", (pid,)).fetchone()[0] == "concluida"
    assert conn.execute("select custo_real > 0 from radar.pesquisas").fetchone()[0]
    crit = conn.execute("select criterios from radar.pontuacoes").fetchone()[0]
    assert {c["nome"]: c["pontos"] for c in crit}["sem_anuncio_ativo"] == 25


async def test_dedup_fechado_e_filtros(conn):
    criar_pesquisa(conn, max_resultados=200, nota_minima=4.0, min_avaliacoes=10)
    ns = [negocio("p1", "Barbearia do Zé"), negocio("p1", "Barbearia do Zé"),
          negocio("p2", "Barbearia Fechada", fechado_permanentemente=True),
          negocio("p3", "Barbearia Nota Baixa", nota=3.2),
          negocio("p4", "Barbearia Longe", lat=-6.5, lng=-42.80),
          negocio("p5", "Barbearia Sem CNPJ")]
    await rodar_tudo(conn, fontes(ns))
    assert sorted(r[0] for r in conn.execute("select place_id from radar.leads")) == ["p1", "p5"]
    # segunda pesquisa com o mesmo negócio: vincula, não duplica
    pid2 = criar_pesquisa(conn, max_resultados=200)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    assert conn.execute("select count(*) from radar.leads").fetchone()[0] == 2
    assert conn.execute("select count(*) from radar.pesquisas_leads where pesquisa_id = %s", (pid2,)).fetchone()[0] == 1
    sem = conn.execute("select cnpj_status, status from radar.leads where place_id = 'p5'").fetchone()
    assert sem == ("nao_encontrado", "pontuado")      # sem CNPJ continua útil


async def test_instagram_bloqueado_nao_trava_o_lead(conn):
    criar_pesquisa(conn, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")], ig=FakeInstagram(falhar=True)))
    lead = conn.execute("select status, instagram_status, anuncio_ativo from radar.leads").fetchone()
    assert lead == ("pontuado", "nao_verificado", "nao_verificado")
    assert conn.execute("select count(*) from radar.jobs where status = 'falhou'").fetchone()[0] == 1


async def test_regra_por_nicho_e_versao(conn):
    conn.execute("""insert into radar.regras_pontuacao (versao, nicho, pesos, faixas)
                    values (2, 'Barbearia', '{"sem_anuncio_ativo": 30}', '{"quente": 70, "morno": 40}')""")
    criar_pesquisa(conn, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    assert conn.execute("select pontuacao, versao_regra from radar.leads").fetchone() == (100, 2)


async def test_recoleta_do_instagram_que_falha_nao_deixa_dado_velho(conn):
    criar_pesquisa(conn, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    lead_id = conn.execute("select id from radar.leads").fetchone()[0]
    assert conn.execute("select instagram_status from radar.leads").fetchone()[0] == "encontrado"
    # humano corrige o @ e o sistema recoleta; a coleta falha de vez
    fila.enfileirar(conn, "enriquecer_instagram", lead_id=lead_id,
                    payload={"instagram": "outro_perfil", "humano": True, "sem_anuncios": True})

    class IgQuebrado(FakeInstagram):
        async def perfil(self, usuario):
            raise RuntimeError("bloqueado")
    await rodar_tudo(conn, fontes([], ig=IgQuebrado()))
    assert conn.execute("select instagram_status from radar.leads").fetchone()[0] == "nao_verificado"


def test_nao_finaliza_com_cnpj_pendente(conn):
    """Com vários robôs, anúncios podem terminar antes do CNPJ: o lead espera."""
    lead_id = conn.execute("""insert into radar.leads (place_id, nome, endereco, link_maps, fonte_maps)
                              values ('px', 'X', 'Rua', 'l', 'f') returning id""").fetchone()[0]
    for tipo, st in (("enriquecer_site", "feito"), ("enriquecer_instagram", "feito"),
                     ("verificar_anuncios", "feito"), ("enriquecer_cnpj", "pendente")):
        conn.execute("insert into radar.jobs (tipo, lead_id, status) values (%s, %s, %s)", (tipo, lead_id, st))
    conn.execute("update radar.jobs set status = 'rodando' where tipo = 'enriquecer_cnpj'")
    conn.execute("update radar.jobs set status = 'feito' where tipo = 'enriquecer_cnpj'")
    assert etapas.talvez_finalizar(conn, lead_id) is True
    conn.execute("update radar.leads set status = 'encontrado' where id = %s", (lead_id,))
    conn.execute("delete from radar.jobs where tipo = 'enriquecer_cnpj'")
    assert etapas.talvez_finalizar(conn, lead_id) is False    # CNPJ nunca rodou: não fecha
