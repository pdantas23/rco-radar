"""Verificação do site do negócio (F5), sem navegador e sem custo.

Portado do ProfileCollector + tech_detect do Harvey (MIT): tenta https/http e
com/sem www, depois casa assinaturas no HTML que já foi baixado. Pixel injetado
só por JavaScript (ex.: via GTM) não aparece aqui — por isso o resultado guarda
`via_gtm` e o modo headless fica para os leads que passaram no filtro inicial.
"""

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from radar.telefone import whatsapp_de_link

TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=8.0, pool=8.0)
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

ASSINATURAS: dict[str, list[str]] = {
    "meta_pixel": [r"connect\.facebook\.net/[^\"']*fbevents\.js", r"fbq\(\s*['\"]init['\"]"],
    "google_tag": [r"googletagmanager\.com/gtag/js\?id=G-", r"google-analytics\.com/analytics\.js",
                   r"gtag\(\s*['\"]config['\"]\s*,\s*['\"]G-"],
    "google_ads": [r"googletagmanager\.com/gtag/js\?id=AW-", r"gtag\(\s*['\"]config['\"]\s*,\s*['\"]AW-",
                   r"googleadservices\.com/pagead/conversion"],
    "gtm": [r"googletagmanager\.com/gtm\.js"],
    "whatsapp": [r"wa\.me/", r"api\.whatsapp\.com/send", r"whatsapp\.com/send"],
}
PLATAFORMAS: dict[str, list[str]] = {
    "WordPress": [r"wp-content/", r"wp-includes/", r'<meta name="generator" content="WordPress'],
    "Wix": [r"static\.parastorage\.com", r"wix\.com/website-builder", r"X-Wix-"],
    "Shopify": [r"cdn\.shopify\.com", r"myshopify\.com"],
    "Nuvemshop": [r"nuvemshop", r"d26lpennugtm8s\.cloudfront\.net", r"tiendanube"],
}
INSTAGRAM_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:[\"'?#]|$)", re.I)
FACEBOOK_RE = re.compile(r"facebook\.com/([A-Za-z0-9_.\-]{3,80})/?(?:[\"'?#]|$)", re.I)
IG_NAO_PERFIL = {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "sharer"}
FB_NAO_PAGINA = {"sharer", "sharer.php", "tr", "plugins", "dialog", "share", "profile.php"}

_comp = {k: [re.compile(p, re.I) for p in v] for k, v in {**ASSINATURAS, **PLATAFORMAS}.items()}


@dataclass
class ResultadoSite:
    site_existe: bool
    site_responde: bool = False
    site_https: bool = False
    site_tem_whatsapp: bool = False
    site_tem_meta_pixel: bool = False
    site_tem_google_tag: bool = False
    site_tem_google_ads: bool = False
    via_gtm: bool = False
    site_plataforma: str = "outro"
    url_final: str = ""
    whatsapp: str | None = None
    instagram_user: str | None = None
    facebook_pagina: str | None = None
    coletado_em: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _tem(chave: str, html: str) -> bool:
    return any(p.search(html) for p in _comp[chave])


def analisar_html(html: str, url_final: str) -> ResultadoSite:
    """Parte pura: tudo que dá para saber do HTML já baixado."""
    amostra = html[:400_000]
    plataforma = next((nome for nome in PLATAFORMAS if _tem(nome, amostra)), "outro")
    ig = next((m.group(1) for m in INSTAGRAM_RE.finditer(amostra)
               if m.group(1).lower() not in IG_NAO_PERFIL), None)
    fb = next((m.group(1) for m in FACEBOOK_RE.finditer(amostra)
               if m.group(1).lower() not in FB_NAO_PAGINA), None)
    return ResultadoSite(
        site_existe=True,
        site_responde=True,
        site_https=urlparse(url_final).scheme == "https",
        site_tem_whatsapp=_tem("whatsapp", amostra),
        site_tem_meta_pixel=_tem("meta_pixel", amostra),
        site_tem_google_tag=_tem("google_tag", amostra),
        site_tem_google_ads=_tem("google_ads", amostra),
        via_gtm=_tem("gtm", amostra),
        site_plataforma=plataforma,
        url_final=url_final,
        whatsapp=whatsapp_de_link(amostra),
        instagram_user=ig,
        facebook_pagina=fb,
    )


def _dominio(url: str) -> str:
    raw = url.strip().lower()
    if "://" not in raw:
        raw = "http://" + raw
    host = (urlparse(raw).hostname or "").strip(".")
    return host[4:] if host.startswith("www.") else host


async def verificar(url: str | None, client: httpx.AsyncClient | None = None) -> ResultadoSite:
    """Baixa a home (https/http, com/sem www) e analisa. Sem URL => site_existe=False."""
    if not url:
        return ResultadoSite(site_existe=False)
    dominio = _dominio(url)
    if not dominio:
        return ResultadoSite(site_existe=False)
    proprio = client is None
    client = client or httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True,
        headers={"User-Agent": random.choice(USER_AGENTS),
                 "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                 "Accept-Language": "pt-BR,pt;q=0.9"})
    try:
        for base in (f"https://{dominio}", f"https://www.{dominio}",
                     f"http://{dominio}", f"http://www.{dominio}"):
            try:
                resp = await client.get(base)
            except (httpx.HTTPError, ValueError):
                continue
            if resp.status_code == 200 and "html" in resp.headers.get("content-type", ""):
                return analisar_html(resp.text, str(resp.url))
        # tem site cadastrado mas nenhuma variante respondeu
        return ResultadoSite(site_existe=True, site_responde=False)
    finally:
        if proprio:
            await client.aclose()
