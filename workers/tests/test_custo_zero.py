"""Modo só dados abertos: toda saída paga falha antes de sair da máquina, mesmo com token configurado."""

import argparse
import importlib

import httpx
import psycopg
import pytest
import respx

from radar import custo_zero, etapas, ia, worker
from radar.comercial import Comercial
from radar.fontes.anuncios_apify import AnunciosApify
from radar.fontes.base import ConsultaMaps
from radar.fontes.cnpj import CnpjCasaDosDados
from radar.fontes.instagram_apify import InstagramApify
from radar.fontes.maps_apify import MapsApify
from radar.fontes.maps_gosom import MapsGosom
from tests.conftest import AGENCIA, DONO
from tests.test_pipeline import FakeCnpj, FakeMaps, FakeInstagram, geo, negocio, rodar_tudo, site_sem_site


@pytest.fixture
def modo(monkeypatch):
    monkeypatch.setenv("RADAR_SO_DADOS_ABERTOS", "1")
    monkeypatch.setenv("APIFY_TOKEN", "token-de-teste")
    monkeypatch.setenv("CASADOSDADOS_API_KEY", "chave-de-teste")


CFG_IA = ia.ConfigIA(True, "anthropic", "claude-haiku-5-5", "chave-de-teste", None, None)


class ClienteIaEspiao:
    """Se a trava falhar, o teste descobre: qualquer uso do cliente levanta outra exceção."""
    def __getattr__(self, nome):
        raise AssertionError("a IA foi chamada no modo só dados abertos")


CHAMADAS_PAGAS = {
    "maps_apify": lambda: MapsApify("t").buscar(ConsultaMaps("x", 0, 0, 1)),
    "instagram_perfil": lambda: InstagramApify("t").perfil("loja"),
    "instagram_procurar": lambda: InstagramApify("t").procurar("loja", "cidade"),
    "anuncios": lambda: AnunciosApify("t").verificar(None, "loja"),
    "casa_dos_dados": lambda: CnpjCasaDosDados("t").buscar_por_nome("Barbearia do Zé", "Teresina", "PI"),
    "ia": lambda: ia.responder(CFG_IA, "RCO", "ficha", [("lead", "oi")], cliente=ClienteIaEspiao()),
    "whatsapp_enviar": lambda: Comercial("https://comercial.exemplo", "s").enviar(
        agency_id="a", lead_id="l", whatsapp_config_id="w", telefone="+55", nome="n", texto="t", primeira=True),
    "whatsapp_passar": lambda: Comercial("https://comercial.exemplo", "s").passar(
        agency_id="a", lead_id="l", conversation_id="c", motivo="m"),
}


@pytest.mark.parametrize("nome", CHAMADAS_PAGAS)
async def test_toda_saida_paga_falha_sem_tocar_na_rede(modo, nome):
    with respx.mock(assert_all_called=False) as rede:
        with pytest.raises(custo_zero.FontePagaBloqueada):
            await CHAMADAS_PAGAS[nome]()
    assert rede.calls.call_count == 0


@respx.mock
async def test_fora_do_modo_a_mesma_chamada_chega_na_rede(monkeypatch):
    """Prova de que o teste acima mede a trava, e não uma falha qualquer."""
    monkeypatch.delenv("RADAR_SO_DADOS_ABERTOS", raising=False)
    rota = respx.post(url__regex=r"https://api\.apify\.com/.*").respond(200, json=[])
    assert await InstagramApify("t").procurar("loja", "cidade") == []
    assert rota.called


@pytest.mark.parametrize("valor,esperado", [("1", True), ("true", True), ("SIM", True), ("0", False), ("", False), ("nao", False)])
def test_valores_da_configuracao(monkeypatch, valor, esperado):
    monkeypatch.setenv("RADAR_SO_DADOS_ABERTOS", valor)
    assert custo_zero.ativo() is esperado


def test_fontes_reais_nao_cria_adaptador_pago_mesmo_com_token(modo):
    f = worker.fontes_reais(None)
    assert isinstance(f.maps, MapsGosom) and f.cnpj.chave == "base_local"
    assert isinstance(f.instagram, custo_zero.FonteBloqueada) and isinstance(f.anuncios, custo_zero.FonteBloqueada)
    assert not f.instagram.habilitada
    assert isinstance(f.mapas["apify_maps"], custo_zero.FonteBloqueada)
    assert not hasattr(f.maps, "reserva")


def test_casa_dos_dados_configurada_impede_a_subida(modo, monkeypatch):
    monkeypatch.setenv("RADAR_FONTE_CNPJ", "casadosdados")
    with pytest.raises(ValueError):
        worker.fontes_reais(None)


def test_contato_fica_desligado_mesmo_com_radar_contato_1(modo, monkeypatch):
    monkeypatch.setenv("RADAR_CONTATO", "1")
    assert worker.contato_ligado() is False


def test_contato_continua_ligado_fora_do_modo(monkeypatch):
    monkeypatch.delenv("RADAR_SO_DADOS_ABERTOS", raising=False)
    monkeypatch.setenv("RADAR_CONTATO", "1")
    assert worker.contato_ligado() is True


def test_gravar_custo_maior_que_zero_falha(modo):
    with pytest.raises(custo_zero.FontePagaBloqueada):
        etapas.custo(None, AGENCIA, "qualquer", "op", 0.01)


def fontes_modo():
    bloq = custo_zero.FonteBloqueada
    return etapas.Fontes(FakeMaps([negocio("p1", "Barbearia do Zé", site="ze.com.br")]), CnpjGratis(),
                         bloq("Instagram"), bloq("Anúncios"), geo, site_sem_site,
                         mapas={"apify_maps": bloq("Maps")}, instagram_site=_arroba)


class CnpjGratis(FakeCnpj):
    custo_unitario_brl = 0.0

    async def buscar_por_nome(self, nome, municipio, uf):
        r = await super().buscar_por_nome(nome, municipio, uf)
        self.custo_ultima_chamada = 0.0
        return r


async def _arroba(url):
    return "barbeariadoze"


def pesquisa(conn, fonte):
    from radar import fila
    pid = conn.execute(
        """insert into radar.pesquisas (agency_id, nome, nicho_texto, lugar_estado, lugar_cidade, raio_km, filtros,
                 fonte_maps, usuario_id, status) values (%s, 't', 'barbearia', 'PI', 'Teresina', 15, '{}', %s, %s, 'na_fila')
           returning id""", (AGENCIA, fonte, DONO)).fetchone()[0]
    fila.enfileirar(conn, "minerar", agency_id=AGENCIA, pesquisa_id=pid)
    return pid


async def test_pipeline_no_modo_termina_com_custo_zero(conn, modo):
    conn.execute("""insert into radar.config (agency_id, chave, valor) values (%s, 'anuncios_ativos', '{"ligado": true}')""", (AGENCIA,))
    pid = pesquisa(conn, "fake_maps")
    await rodar_tudo(conn, fontes_modo())
    assert conn.execute("select status, instagram_status, anuncio_ativo, cnpj_status from radar.leads").fetchone() == \
        ("pontuado", "encontrado", "nao_verificado", "encontrado")
    assert conn.execute("select count(*) from radar.jobs where tipo = 'verificar_anuncios'").fetchone()[0] == 0
    assert conn.execute("select coalesce(sum(valor), 0) from radar.custos_api").fetchone()[0] == 0
    assert conn.execute("select status, custo_real from radar.pesquisas where id = %s", (pid,)).fetchone() == ("concluida", 0)


async def test_pesquisa_marcada_para_o_apify_falha_e_nao_troca_de_fonte(conn, modo):
    pid = pesquisa(conn, "apify_maps")
    f = fontes_modo()
    await worker.processar_um(conn, f, "teste")
    assert conn.execute("select status from radar.pesquisas where id = %s", (pid,)).fetchone()[0] == "falhou"
    st, erro, tent = conn.execute("select status, erro, tentativas from radar.jobs where tipo = 'minerar'").fetchone()
    assert st == "falhou" and "dados abertos" in erro and tent == 1
    assert conn.execute("select count(*) from radar.custos_api").fetchone()[0] == 0


async def test_job_de_anuncios_ja_na_fila_falha_sem_gastar(conn, modo):
    from radar import fila
    pesquisa(conn, "fake_maps")
    await rodar_tudo(conn, fontes_modo())
    lead = conn.execute("select id from radar.leads").fetchone()[0]
    fila.enfileirar(conn, "verificar_anuncios", agency_id=AGENCIA, lead_id=lead, payload={"pagina": "x"})
    await worker.processar_um(conn, fontes_modo(), "teste")
    assert conn.execute("select status from radar.jobs where tipo = 'verificar_anuncios'").fetchone()[0] == "falhou"
    assert conn.execute("select anuncio_ativo from radar.leads").fetchone()[0] == "nao_verificado"
    assert conn.execute("select coalesce(sum(valor), 0) from radar.custos_api").fetchone()[0] == 0


# ── rodada de teste ──────────────────────────────────────────────────────
async def test_rodada_mostra_o_que_seria_gravado_e_nao_grava(conn, monkeypatch):
    monkeypatch.setenv("RADAR_SO_DADOS_ABERTOS", "0")                  # a rodada liga o modo por conta própria
    rodada = importlib.import_module("radar.rodada_dados_abertos")
    from tests.conftest import DSN
    ro = psycopg.connect(DSN, autocommit=True, options="-c default_transaction_read_only=on")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        ro.execute("insert into radar.cnpj_base (cnpj, razao_social, uf, municipio, nome_busca) values ('1','x','PI','y','z')")
    args = argparse.Namespace(termo="barbearia", cidade="Teresina", uf="PI", limite=5, raio_km=5, lat=-5.0, lng=-42.0)
    itens = await rodada.rodar(args, ro, maps=FakeMaps([negocio("p1", "Barbearia do Zé", site="ze.com.br")]), cnpj=CnpjGratis(),
                               site=site_sem_site, instagram_site=_arroba)
    ro.close()
    assert custo_zero.ativo()
    assert itens[0]["radar.leads"]["cnpj_status"] == "encontrado"
    assert itens[0]["radar.presenca"]["instagram_user"] == "barbeariadoze"
    assert rodada.resumo(itens)["custo_brl"] == 0
    assert conn.execute("select count(*) from radar.leads").fetchone()[0] == 0
    monkeypatch.delenv("RADAR_SO_DADOS_ABERTOS", raising=False)
