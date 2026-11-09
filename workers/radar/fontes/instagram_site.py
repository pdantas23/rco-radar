"""F5 · Acha o @ do Instagram no site da empresa, sem custo: roda antes de qualquer busca paga.

O link vem do Google Maps, então é dado não confiável: timeout curto, corpo limitado, só http/https,
nunca endereço interno e redirecionamento só dentro do próprio domínio. Qualquer falha devolve None.
"""

import asyncio
import ipaddress
import re
import socket
from collections import Counter
from urllib.parse import urljoin, urlparse

import httpx

TIMEOUT = httpx.Timeout(connect=4.0, read=5.0, write=4.0, pool=4.0)
TEMPO_TOTAL_SEG = 10.0
MAX_BYTES = 400_000
MAX_REDIRECIONAMENTOS = 3
CABECALHOS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
              "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "pt-BR,pt;q=0.9"}

# caminhos do próprio Instagram (post, reel, etc.) e contas de quem fez o site, nada disso é o negócio
NAO_PERFIL = {"p", "reel", "reels", "tv", "explore", "stories", "accounts", "sharer", "about", "legal", "developer",
              "directory", "web", "oauth", "privacy", "terms", "direct", "help", "api", "share", "_u",
              "wix", "wordpress", "shopify", "nuvemshop", "tiendanube", "canva", "hostinger", "elementor"}
# aceita barra escapada de JSON embutido (instagram.com\/usuario\/)
LINK_RE = re.compile(r"instagram\.com(?:\\?/)+([A-Za-z0-9_.]{1,30})(?P<seg>\\?/[A-Za-z0-9_\-.]+)?", re.I)
USUARIO_RE = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?$")


def _valido(usuario: str) -> bool:
    return bool(USUARIO_RE.match(usuario)) and usuario.lower() not in NAO_PERFIL and ".." not in usuario


def usuario_do_link(url: str | None) -> str | None:
    """'https://instagram.com/loja/?hl=pt' -> 'loja'. Post, reel e páginas do próprio Instagram -> None."""
    if not url:
        return None
    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    if host != "instagram.com" and not host.endswith(".instagram.com"):
        return None
    partes = [p for p in u.path.split("/") if p]
    return partes[0] if len(partes) == 1 and _valido(partes[0]) else None


def extrair_de_html(html: str) -> str | None:
    """O @ mais citado na página; no empate, o primeiro. Link com caminho a mais (post/reel) não conta."""
    achados = [m.group(1) for m in LINK_RE.finditer(html[:MAX_BYTES]) if not m.group("seg") and _valido(m.group(1))]
    if not achados:
        return None
    contagem = Counter(a.lower() for a in achados)
    return max(achados, key=lambda a: (contagem[a.lower()], -achados.index(a)))


def mesmo_dominio(a: str | None, b: str | None) -> bool:
    a, b = (a or "").lower().removeprefix("www."), (b or "").lower().removeprefix("www.")
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _resolver(host: str) -> list[str]:
    return [i[4][0] for i in socket.getaddrinfo(host, None)]


async def _host_publico(host: str, resolver) -> bool:
    try:
        ips = await asyncio.to_thread(resolver, host)
        return bool(ips) and all(ipaddress.ip_address(ip).is_global for ip in ips)
    except (OSError, ValueError):
        return False


async def _baixar(client: httpx.AsyncClient, url: str) -> tuple[int, str | None, str]:
    """(status, destino do redirecionamento, html até o limite)."""
    async with client.stream("GET", url, headers=CABECALHOS) as r:
        if r.is_redirect:
            return r.status_code, r.headers.get("location"), ""
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "").lower():
            return r.status_code, None, ""
        corpo = bytearray()
        async for parte in r.aiter_bytes():
            corpo.extend(parte)
            if len(corpo) >= MAX_BYTES:
                break
        return 200, None, bytes(corpo[:MAX_BYTES]).decode(r.encoding or "utf-8", errors="ignore")


async def _procurar(url: str, client: httpx.AsyncClient, resolver) -> str | None:
    origem = urlparse(url).hostname
    atual = url
    for _ in range(MAX_REDIRECIONAMENTOS + 1):
        p = urlparse(atual)
        if p.scheme not in ("http", "https") or not p.hostname or not await _host_publico(p.hostname, resolver):
            return None
        status, destino, html = await _baixar(client, atual)
        if html:
            return extrair_de_html(html)
        if not destino:
            return None
        atual = urljoin(atual, destino)
        if perfil := usuario_do_link(atual):         # o site aponta direto para o perfil
            return perfil
        if not mesmo_dominio(origem, urlparse(atual).hostname):
            return None
    return None


async def instagram_do_site(url: str | None, client: httpx.AsyncClient | None = None,
                            resolver=_resolver) -> str | None:
    """Devolve o @ ou None; nunca levanta exceção de rede (site fora do ar não trava o lead)."""
    if not url or not url.strip():
        return None
    url = url.strip()
    url = url if "://" in url else "https://" + url
    if perfil := usuario_do_link(url):              # o Maps muitas vezes traz o próprio Instagram como "site"
        return perfil
    if (urlparse(url).hostname or "").lower().endswith("instagram.com"):
        return None
    proprio = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)
    try:
        return await asyncio.wait_for(_procurar(url, client, resolver), TEMPO_TOTAL_SEG)
    except (httpx.HTTPError, httpx.InvalidURL, asyncio.TimeoutError, UnicodeError, ValueError):
        return None
    finally:
        if proprio:
            await client.aclose()
