"""Pipeline completo (minerar -> enriquecer -> pontuar) contra Postgres real, com fontes falsas.

Banco: stubs do COMERCIAL + migrations 001–003 (escopo por agência). Ver tests/conftest.py.
"""

from datetime import date, datetime, timezone

import psycopg

from radar import etapas, fila
from radar.fontes.base import Avaliacao, DadosCnpj, NegocioMaps, PerfilInstagram
from radar.fontes.maps_gosom import FonteMapsComReserva
from radar.site import ResultadoSite
from radar.worker import processar_um
from tests.conftest import AGENCIA, DONO, OUTRA_AGENCIA


def negocio(pid, nome, **kw):
    base = dict(place_id=pid, nome=nome, categoria="Barbearia", endereco="R. A, 100 - Centro, Teresina - PI",
                cidade="Teresina", estado="PI", cep="64000-000", lat=-5.09, lng=-42.80,
                telefone="(86) 99999-8888", site=None, nota=4.6, qtd_avaliacoes=180, horario=None,
                link_maps=f"https://maps.google.com/?cid={pid}", fechado_permanentemente=False,
                bruto={"title": nome, "reviews": [{"text": "ótimo", "stars": 5}]},
                avaliacoes=[Avaliacao("", 5, f"ótimo {i}", datetime(2026, 10, i + 1, tzinfo=timezone.utc)) for i in range(7)])
    base.update(kw)
    return NegocioMaps(**base)


class Fake:
    custo_ultima_chamada = 0.0
    custo_unitario_brl = 0.01

    def estimar(self, n):
        return round(n * self.custo_unitario_brl, 4)


class FakeMaps(Fake):
    chave, custo_unitario_brl = "fake_maps", 0.0     # como o gosom: grátis

    def __init__(self, negocios):
        self.negocios = negocios

    async def buscar(self, consulta, uf_padrao=""):
        return list(self.negocios)


class FakeMapsPago(FakeMaps):
    chave, custo_unitario_brl = "fake_maps_pago", 0.03

    async def buscar(self, consulta, uf_padrao=""):
        self.custo_ultima_chamada = 0.03 * len(self.negocios)
        return list(self.negocios)


class FakeCnpj(Fake):
    chave = "fake_cnpj"

    async def buscar_por_nome(self, nome, municipio, uf):
        self.custo_ultima_chamada = 0.01
        if "Zé" not in nome:
            return []
        return [DadosCnpj(cnpj="11111111000111", razao_social="JOSE S SILVA ME", nome_fantasia="BARBEARIA DO ZE",
                          situacao="ATIVA", abertura=date(2019, 3, 1), porte="ME", natureza="Empresário",
                          capital_social=10000, cnae_principal="9602501", cnae_descricao="Cabeleireiros",
                          cnaes_secundarios=[], regime="Simples", socios=[{"nome": "JOSE", "qualificacao": "Titular"}],
                          email_rf=None, telefone_rf=None, logradouro="RUA A", numero="100", municipio="TERESINA",
                          uf="PI", cep="64000000", eh_filial=False, cnpj_matriz="111111110001",
                          bruto={"casa": {"razao_social": "JOSE S SILVA ME"}})]

    async def detalhar(self, cnpj):
        return {"email": "ZE@receita.com", "opcao_pelo_simples": True, "cnae_fiscal": 9602501}


class FakeInstagram(Fake):
    chave = "fake_ig"

    def __init__(self, falhar=False):
        self.falhar = falhar
        self.chamadas = 0

    async def procurar(self, nome, cidade):
        self.chamadas += 1
        if self.falhar:
            raise RuntimeError("bloqueado pelo Instagram")
        return ["barbeariadoze"] if "Zé" in nome else []

    async def perfil(self, usuario):
        self.chamadas += 1
        self.custo_ultima_chamada = 0.01
        return PerfilInstagram(usuario, 2300, 300, 400, datetime(2026, 10, 15, tzinfo=timezone.utc), 9,
                               "Agende pelo WhatsApp", True, True, True,
                               bruto={"username": usuario, "fullName": "Barbearia do Zé"})


class FakeAnuncios(Fake):
    chave = "fake_ads"

    async def verificar(self, pagina, nome, pais="BR"):
        from radar.fontes.base import ResultadoAnuncios
        self.custo_ultima_chamada = 0.004
        return ResultadoAnuncios(None, pagina, 0, None, None, [])


async def site_sem_site(url):
    return ResultadoSite(site_existe=False)


async def geo(cidade, uf, agency_id):
    return (-5.09, -42.80)


def criar_pesquisa(conn, agency_id=AGENCIA, **filtros) -> str:
    pid = conn.execute(
        """insert into radar.pesquisas (agency_id, nome, nicho_texto, lugar_estado, lugar_cidade, raio_km, filtros,
                 fonte_maps, usuario_id, status) values (%s, 'barbearia · Teresina', 'barbearia', 'PI', 'Teresina',
                 15, %s, 'fake_maps', %s, 'na_fila') returning id""",
        (agency_id, psycopg.types.json.Jsonb(filtros), DONO)).fetchone()[0]
    fila.enfileirar(conn, "minerar", agency_id=agency_id, pesquisa_id=pid)
    return pid


async def rodar_tudo(conn, f, limite=200):
    for _ in range(limite):
        if not await processar_um(conn, f, "teste"):
            return
        conn.execute("update radar.jobs set agendado_para = now() where status = 'pendente'")  # pula backoff
    raise AssertionError("fila não esvaziou")


def fontes(negocios, ig=None, maps=None):
    return etapas.Fontes(maps or FakeMaps(negocios), FakeCnpj(), ig or FakeInstagram(), FakeAnuncios(), geo,
                         site_sem_site)


async def test_exemplo_do_levantamento_vira_quente(conn):
    pid = criar_pesquisa(conn, max_resultados=200, nota_minima=4.0)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    lead = conn.execute("""select status, pontuacao, classificacao, anuncio_ativo, presenca_digital,
                                  faturamento_estimado_faixa, cnpj_status, tem_whatsapp_provavel, telefone, agency_id::text
                           from radar.leads""").fetchone()
    assert lead == ("pontuado", 100, "quente", "nao_verificado", "media", "30k_80k", "encontrado", True,
                    "+5586999998888", AGENCIA)
    assert conn.execute("select count(*) from radar.jobs where tipo = 'verificar_anuncios'").fetchone()[0] == 0
    assert conn.execute("select count(*) from radar.avaliacoes").fetchone()[0] == 5
    assert conn.execute("select status from radar.pesquisas where id = %s", (pid,)).fetchone()[0] == "concluida"
    # custo por agência, visto pela view do teto
    gasto = conn.execute("select gasto_brl from radar.gasto_mes where agency_id = %s", (AGENCIA,)).fetchone()[0]
    assert gasto > 0
    crit = conn.execute("select criterios from radar.pontuacoes").fetchone()[0]
    assert {c["nome"]: c["pontos"] for c in crit}["tem_whatsapp"] == 25
    maps, ig = conn.execute("""select l.dados_maps, p.dados_instagram from radar.leads l
                               join radar.presenca p on p.lead_id = l.id and p.dados_instagram is not null""").fetchone()
    assert maps["title"] == "Barbearia do Zé" and ig == {"username": "barbeariadoze", "fullName": "Barbearia do Zé"}
    assert conn.execute("select email, email_origem from radar.leads").fetchone() == ("ze@receita.com", "receita")


async def test_dedup_fechado_e_filtros(conn):
    criar_pesquisa(conn, max_resultados=200, nota_minima=4.0, min_avaliacoes=10)
    ns = [negocio("p1", "Barbearia do Zé"), negocio("p1", "Barbearia do Zé"),
          negocio("p2", "Barbearia Fechada", fechado_permanentemente=True),
          negocio("p3", "Barbearia Nota Baixa", nota=3.2),
          negocio("p4", "Barbearia Longe", lat=-6.5, lng=-42.80),
          negocio("p5", "Barbearia Sem CNPJ")]
    await rodar_tudo(conn, fontes(ns))
    assert sorted(r[0] for r in conn.execute("select place_id from radar.leads")) == ["p1", "p5"]
    pid2 = criar_pesquisa(conn, max_resultados=200)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    assert conn.execute("select count(*) from radar.leads").fetchone()[0] == 2
    assert conn.execute("select count(*) from radar.pesquisas_leads where pesquisa_id = %s", (pid2,)).fetchone()[0] == 1
    assert conn.execute("select cnpj_status, status from radar.leads where place_id = 'p5'").fetchone() == \
        ("nao_encontrado", "pontuado")


async def test_duas_agencias_o_mesmo_negocio(conn):
    """Cada agência tem o seu lead do mesmo negócio; nada vaza de uma para a outra."""
    criar_pesquisa(conn, AGENCIA, max_resultados=50)
    criar_pesquisa(conn, OUTRA_AGENCIA, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    linhas = conn.execute("select agency_id::text, status from radar.leads order by agency_id").fetchall()
    assert linhas == [(AGENCIA, "pontuado"), (OUTRA_AGENCIA, "pontuado")]
    for tabela in ("empresas", "presenca", "avaliacoes", "pontuacoes"):
        cruzado = conn.execute(f"""select count(*) from radar.{tabela} t join radar.leads l on l.id = t.lead_id
                                   where t.agency_id <> l.agency_id""").fetchone()[0]
        assert cruzado == 0, tabela


async def test_radar_desligado_cancela_sem_gastar(conn):
    conn.execute("update radar.agencias set ativo = false where agency_id = %s", (AGENCIA,))
    pid = criar_pesquisa(conn, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    assert conn.execute("select status from radar.pesquisas where id = %s", (pid,)).fetchone()[0] == "cancelada"
    assert conn.execute("select status from radar.jobs").fetchone()[0] == "cancelado"
    assert conn.execute("select count(*) from radar.leads").fetchone()[0] == 0
    assert conn.execute("select count(*) from radar.custos_api").fetchone()[0] == 0


async def test_sem_teto_maps_gratis_roda_e_pagos_ficam_nao_verificados(conn):
    conn.execute("update radar.agencias set teto_mensal_brl = 0 where agency_id = %s", (AGENCIA,))
    criar_pesquisa(conn, max_resultados=50)
    ig = FakeInstagram()
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")], ig=ig))
    lead = conn.execute("select status, cnpj_status, instagram_status from radar.leads").fetchone()
    assert lead == ("pontuado", "nao_verificado", "nao_verificado")
    assert ig.chamadas == 0                                    # nenhuma chamada paga
    assert conn.execute("select coalesce(sum(valor), 0) from radar.custos_api").fetchone()[0] == 0


async def test_teto_para_a_busca_paga_no_meio(conn):
    conn.execute("update radar.agencias set teto_mensal_brl = 0.05 where agency_id = %s", (AGENCIA,))
    criar_pesquisa(conn, max_resultados=500)                  # muitas células
    maps = FakeMapsPago([negocio(f"p{i}", f"Barbearia {i}") for i in range(3)])
    await rodar_tudo(conn, fontes([], maps=maps))
    buscas = conn.execute("select count(*) from radar.custos_api where operacao = 'buscar'").fetchone()[0]
    assert buscas <= 2                                        # parou ao bater o teto, não rodou todas as células


async def test_reserva_paga_so_com_teto(conn):
    class Quebrado(FakeMaps):
        async def buscar(self, consulta, uf_padrao=""):
            raise RuntimeError("gosom fora")
    reserva = FakeMapsPago([negocio("p1", "Barbearia do Zé")])
    conn.execute("update radar.agencias set teto_mensal_brl = 0 where agency_id = %s", (AGENCIA,))
    pid = criar_pesquisa(conn, max_resultados=20)
    await rodar_tudo(conn, fontes([], maps=FonteMapsComReserva(Quebrado([]), reserva)))
    assert conn.execute("select status from radar.pesquisas where id = %s", (pid,)).fetchone()[0] == "falhou"
    assert conn.execute("select count(*) from radar.leads").fetchone()[0] == 0
    conn.execute("update radar.agencias set teto_mensal_brl = 10 where agency_id = %s", (AGENCIA,))
    criar_pesquisa(conn, max_resultados=20)
    await rodar_tudo(conn, fontes([], maps=FonteMapsComReserva(Quebrado([]), reserva)))
    assert conn.execute("select fonte from radar.custos_api where operacao = 'buscar'").fetchone()[0] == "fake_maps_pago"


async def test_instagram_bloqueado_nao_trava_o_lead(conn):
    criar_pesquisa(conn, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")], ig=FakeInstagram(falhar=True)))
    assert conn.execute("select status, instagram_status, anuncio_ativo from radar.leads").fetchone() == \
        ("pontuado", "nao_verificado", "nao_verificado")
    assert conn.execute("select count(*) from radar.jobs where status = 'falhou'").fetchone()[0] == 1


async def test_regra_da_agencia_vence_a_padrao_rco(conn):
    conn.execute("""insert into radar.regras_pontuacao (agency_id, versao, nicho, pesos, faixas)
                    values (null, 9, 'Barbearia', '{"tem_whatsapp": 50}', '{"quente": 70, "morno": 40}'),
                           (%s, 2, 'Barbearia', '{"tem_whatsapp": 10, "presenca_media_ou_forte": 20,
                            "faturamento_30k_ou_mais": 20, "google_bem_avaliado": 20, "cnpj_ativo": 15}',
                            '{"quente": 70, "morno": 40}')""", (AGENCIA,))
    criar_pesquisa(conn, AGENCIA, max_resultados=50)
    criar_pesquisa(conn, OUTRA_AGENCIA, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    pts = dict(conn.execute("select agency_id::text, pontuacao from radar.leads").fetchall())
    assert pts[AGENCIA] == 85            # regra própria da agência (v2)
    assert pts[OUTRA_AGENCIA] == 50      # sem regra própria: padrão RCO do nicho (v9)


async def test_regra_salva_vale_como_esta(conn):
    conn.execute("""insert into radar.regras_pontuacao (agency_id, versao, nicho, pesos, faixas)
                    values (%s, 5, null, '{"tem_whatsapp": 25, "presenca_media_ou_forte": 20,
                            "faturamento_30k_ou_mais": 20, "google_bem_avaliado": 20}', '{"quente": 70, "morno": 40}')""",
                 (AGENCIA,))
    criar_pesquisa(conn, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    assert conn.execute("select pontuacao from radar.leads").fetchone()[0] == 85
    crit = conn.execute("select criterios from radar.pontuacoes").fetchone()[0]
    assert "cnpj_ativo" not in {c["nome"] for c in crit}


async def test_anuncios_ligados_entram_no_fluxo(conn):
    conn.execute("""insert into radar.config (agency_id, chave, valor)
                    values (%s, 'anuncios_ativos', '{"ligado": true}')""", (AGENCIA,))
    criar_pesquisa(conn, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    assert conn.execute("select status, anuncio_ativo from radar.leads").fetchone() == ("pontuado", "nao")


async def test_email_do_site_tem_prioridade_sobre_o_da_receita(conn):
    async def site_com_email(url):
        return ResultadoSite(site_existe=True, site_responde=True, emails=["contato@ze.com.br"])
    criar_pesquisa(conn, max_resultados=50)
    f = fontes([negocio("p1", "Barbearia do Zé", site="ze.com.br")])
    f.site = site_com_email
    await rodar_tudo(conn, f)
    assert conn.execute("select email, email_origem from radar.leads").fetchone() == ("contato@ze.com.br", "site")


async def test_recoleta_do_instagram_que_falha_nao_deixa_dado_velho(conn):
    criar_pesquisa(conn, max_resultados=50)
    await rodar_tudo(conn, fontes([negocio("p1", "Barbearia do Zé")]))
    lead_id = conn.execute("select id from radar.leads").fetchone()[0]
    assert conn.execute("select instagram_status from radar.leads").fetchone()[0] == "encontrado"
    fila.enfileirar(conn, "enriquecer_instagram", agency_id=AGENCIA, lead_id=lead_id,
                    payload={"instagram": "outro_perfil", "humano": True, "sem_anuncios": True})

    class IgQuebrado(FakeInstagram):
        async def perfil(self, usuario):
            raise RuntimeError("bloqueado")
    await rodar_tudo(conn, fontes([], ig=IgQuebrado()))
    assert conn.execute("select instagram_status from radar.leads").fetchone()[0] == "nao_verificado"


def _lead_cru(conn, place_id):
    return conn.execute("""insert into radar.leads (agency_id, place_id, nome, endereco, link_maps, fonte_maps)
                           values (%s, %s, 'X', 'Rua', 'l', 'f') returning id""", (AGENCIA, place_id)).fetchone()[0]


def test_nao_finaliza_com_cnpj_pendente(conn):
    lead_id = _lead_cru(conn, "px")
    for tipo in ("enriquecer_site", "enriquecer_instagram", "enriquecer_cnpj"):
        conn.execute("insert into radar.jobs (agency_id, tipo, lead_id, status) values (%s, %s, %s, 'feito')",
                     (AGENCIA, tipo, lead_id))
    assert etapas.talvez_finalizar(conn, lead_id) is True
    conn.execute("update radar.leads set status = 'encontrado' where id = %s", (lead_id,))
    conn.execute("delete from radar.jobs where tipo in ('enriquecer_cnpj', 'pontuar')")
    assert etapas.talvez_finalizar(conn, lead_id) is False    # CNPJ nunca rodou: não fecha


def test_nao_finaliza_antes_do_instagram(conn):
    lead_id = _lead_cru(conn, "py")
    for tipo in ("enriquecer_site", "enriquecer_cnpj"):
        conn.execute("insert into radar.jobs (agency_id, tipo, lead_id, status) values (%s, %s, %s, 'feito')",
                     (AGENCIA, tipo, lead_id))
    assert etapas.talvez_finalizar(conn, lead_id) is False
    conn.execute("""insert into radar.jobs (agency_id, tipo, lead_id, status)
                    values (%s, 'enriquecer_instagram', %s, 'feito')""", (AGENCIA, lead_id))
    assert etapas.talvez_finalizar(conn, lead_id) is True
