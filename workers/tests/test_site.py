import httpx
import respx

from radar.site import analisar_html, verificar

HTML_WP = """<html><head>
<link rel='stylesheet' href='/wp-content/themes/x/style.css'>
<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC123"></script>
<script>gtag('config', 'AW-999');</script>
<script>!function(f,b,e,v,n,t,s){}(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');fbq('init','1');</script>
</head><body>
<a href="https://wa.me/5586999998888">WhatsApp</a>
<a href="https://www.instagram.com/p/abc/">post</a>
<a href="https://www.instagram.com/barbeariadoze/">Instagram</a>
<a href="https://www.facebook.com/sharer.php?u=x">share</a>
<a href="https://www.facebook.com/barbeariadoze">Facebook</a>
</body></html>"""


def test_analisa_tudo():
    r = analisar_html(HTML_WP, "https://barbeariadoze.com.br/")
    assert r.site_https and r.site_plataforma == "WordPress"
    assert r.site_tem_meta_pixel and r.site_tem_google_tag and r.site_tem_google_ads
    assert r.site_tem_whatsapp and r.whatsapp == "+5586999998888"
    assert r.instagram_user == "barbeariadoze"       # pula /p/
    assert r.facebook_pagina == "barbeariadoze"      # pula sharer


def test_site_vazio():
    r = analisar_html("<html><body>Oi</body></html>", "http://x.com.br/")
    assert not r.site_https and not r.site_tem_meta_pixel and r.site_plataforma == "outro"


async def test_sem_url():
    r = await verificar(None)
    assert r.site_existe is False


@respx.mock
async def test_cai_para_http_quando_https_falha():
    respx.get("https://loja.com.br").mock(side_effect=httpx.ConnectError("tls"))
    respx.get("https://www.loja.com.br").mock(side_effect=httpx.ConnectError("tls"))
    respx.get("http://loja.com.br").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html>cdn.shopify.com</html>"))
    r = await verificar("https://www.loja.com.br/contato")
    assert r.site_responde and not r.site_https and r.site_plataforma == "Shopify"


@respx.mock
async def test_site_fora_do_ar():
    respx.route(host__regex=r".*fora\.com\.br").mock(return_value=httpx.Response(503))
    r = await verificar("fora.com.br")
    assert r.site_existe and not r.site_responde


def test_emails_e_redes():
    html = """<a href="mailto:Contato@BarbeariaDoZe.com.br">e-mail</a> seuemail@gmail.com
    logo@2x.png <a href="https://www.tiktok.com/@barbeariadoze">tt</a>
    <a href="https://youtube.com/@barbeariadoze">yt</a> <a href="https://www.instagram.com/barbeariadoze/">ig</a>"""
    r = analisar_html(html, "https://ze.com.br/")
    assert r.emails == ["contato@barbeariadoze.com.br"]
    assert r.redes == {"tiktok": "barbeariadoze", "youtube": "barbeariadoze", "instagram": "barbeariadoze"}


@respx.mock
async def test_pagina_de_contato_completa_a_home():
    # url__eq: sem isso o respx casa "https://ze.com.br" com qualquer caminho do host
    respx.get(url__eq="https://ze.com.br/").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html>home sem contato</html>"))
    contato = "<html>" + "x" * 600 + '<a href="https://wa.me/5586999998888">zap</a> fale@ze.com.br</html>'
    respx.get("https://ze.com.br/contato").mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text=contato))
    respx.route(host="ze.com.br").mock(return_value=httpx.Response(404))
    r = await verificar("ze.com.br")
    assert r.whatsapp == "+5586999998888" and r.emails == ["fale@ze.com.br"] and r.site_tem_whatsapp
