"""Extração do @ a partir do site: formatos de link, redirecionamento, falhas e segurança."""

import httpx
import pytest
import respx

from radar.fontes.instagram_site import extrair_de_html, instagram_do_site, mesmo_dominio, usuario_do_link
from radar.site import verificar

PUBLICO = lambda host: ["8.8.8.8"]        # noqa: E731
HTML = {"content-type": "text/html; charset=utf-8"}


@pytest.mark.parametrize("url,usuario", [
    ("https://www.instagram.com/barbeariadoze/", "barbeariadoze"),
    ("https://instagram.com/barbearia.do_ze?hl=pt-br", "barbearia.do_ze"),
    ("instagram.com/loja", "loja"),
    ("https://www.instagram.com/p/C8abc123/", None),
    ("https://www.instagram.com/reel/C8abc123/", None),
    ("https://www.instagram.com/reels/", None),
    ("https://www.instagram.com/stories/loja/123/", None),
    ("https://www.instagram.com/loja/reel/C8abc/", None),
    ("https://www.instagram.com/", None),
    ("https://evil.com/instagram.com/loja", None),
    ("https://instagram.com.evil.com/loja", None),
])
def test_usuario_do_link(url, usuario):
    assert usuario_do_link(url) == usuario


@pytest.mark.parametrize("html,usuario", [
    ('<a href="https://www.instagram.com/lojadamaria/">ig</a>', "lojadamaria"),
    ('<a href="https://instagram.com/lojadamaria?igsh=abc">', "lojadamaria"),
    ('{"link":"https:\\/\\/www.instagram.com\\/lojadamaria\\/"}', "lojadamaria"),
    ('<a href="https://www.instagram.com/p/AbC123/">post</a>', None),
    ('<a href="https://www.instagram.com/reel/AbC123/">reel</a>', None),
    ('<a href="https://www.instagram.com/lojadamaria/p/AbC123/">post</a>', None),
    ('<a href="https://www.instagram.com/wix/">quem fez</a>', None),
    ("<p>sem rede nenhuma</p>", None),
])
def test_extrair_de_html(html, usuario):
    assert extrair_de_html(html) == usuario


def test_post_nao_atrapalha_o_perfil_e_mais_citado_vence():
    html = ('<a href="https://instagram.com/reel/x1/"></a><a href="https://instagram.com/agencia/"></a>'
            '<a href="https://instagram.com/lojadamaria/"></a><a href="https://instagram.com/lojadamaria"></a>')
    assert extrair_de_html(html) == "lojadamaria"


def test_mesmo_dominio():
    assert mesmo_dominio("www.loja.com.br", "loja.com.br") and mesmo_dominio("loja.com.br", "m.loja.com.br")
    assert not mesmo_dominio("loja.com.br", "outraloja.com.br") and not mesmo_dominio("loja.com.br", "parking.io")


async def rodar(url):
    async with httpx.AsyncClient(follow_redirects=False) as c:
        return await instagram_do_site(url, client=c, resolver=PUBLICO)


async def test_link_do_maps_ja_e_o_instagram_sem_rede():
    with respx.mock(assert_all_called=False) as m:
        assert await instagram_do_site("https://instagram.com/lojadamaria/") == "lojadamaria"
    assert m.calls.call_count == 0


@respx.mock
async def test_acha_na_home():
    respx.get("https://loja.com.br/").respond(200, headers=HTML, text='<a href="https://instagram.com/lojadamaria/">')
    assert await rodar("https://loja.com.br/") == "lojadamaria"


@respx.mock
async def test_redirecionamento_no_mesmo_dominio_segue():
    respx.get("http://loja.com.br/").respond(301, headers={"location": "https://www.loja.com.br/"})
    respx.get("https://www.loja.com.br/").respond(200, headers=HTML, text='"https://instagram.com/lojadamaria"')
    assert await rodar("http://loja.com.br/") == "lojadamaria"


@respx.mock
async def test_nao_segue_dominio_estranho():
    respx.get("https://loja.com.br/").respond(302, headers={"location": "https://estacionado.io/x"})
    alvo = respx.get("https://estacionado.io/x").respond(200, headers=HTML, text='"https://instagram.com/golpe"')
    assert await rodar("https://loja.com.br/") is None
    assert not alvo.called


@respx.mock
async def test_redirecionamento_direto_para_o_perfil():
    respx.get("https://loja.com.br/").respond(301, headers={"location": "https://www.instagram.com/lojadamaria/"})
    assert await rodar("https://loja.com.br/") == "lojadamaria"


@respx.mock
async def test_laco_de_redirecionamento_termina():
    respx.get("https://loja.com.br/a").respond(302, headers={"location": "/a"})
    assert await rodar("https://loja.com.br/a") is None


@respx.mock
@pytest.mark.parametrize("efeito", [httpx.ConnectTimeout("x"), httpx.ReadTimeout("x"), httpx.ConnectError("x")])
async def test_falha_de_rede_devolve_none(efeito):
    respx.get("https://loja.com.br/").mock(side_effect=efeito)
    assert await rodar("https://loja.com.br/") is None


@respx.mock
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_erro_http_devolve_none(status):
    respx.get("https://loja.com.br/").respond(status, headers=HTML, text='"https://instagram.com/x1"')
    assert await rodar("https://loja.com.br/") is None


@respx.mock
async def test_pdf_nao_e_lido():
    respx.get("https://loja.com.br/").respond(200, headers={"content-type": "application/pdf"}, content=b"instagram.com/x1")
    assert await rodar("https://loja.com.br/") is None


@respx.mock
async def test_corpo_gigante_para_no_limite():
    respx.get("https://loja.com.br/").respond(200, headers=HTML, text="a" * 2_000_000 + '"https://instagram.com/tarde"')
    assert await rodar("https://loja.com.br/") is None


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/latest/", "http://10.0.0.5/admin",
                                 "http://[::1]/", "ftp://loja.com.br/", "http://localhost/"])
async def test_endereco_interno_nao_e_acessado(url):
    def resolver(host):
        return ["127.0.0.1"] if host == "localhost" else [host]
    with respx.mock(assert_all_called=False) as m:
        async with httpx.AsyncClient() as c:
            assert await instagram_do_site(url, client=c, resolver=resolver) is None
    assert m.calls.call_count == 0


@respx.mock
async def test_verificar_descarta_o_arroba_se_redirecionou_para_outro_dominio():
    respx.get("https://loja.com.br").respond(301, headers={"location": "https://outro.com/"})
    respx.get("https://outro.com/").respond(200, headers=HTML, text='<a href="https://instagram.com/golpe/">')
    respx.get(url__regex=r"https://outro\.com/.+").respond(404)
    async with httpx.AsyncClient(follow_redirects=True) as c:
        r = await verificar("https://loja.com.br", client=c)
    assert r.instagram_user is None and r.site_responde
