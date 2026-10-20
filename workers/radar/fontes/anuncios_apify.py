"""F6 · Anúncios ativos na Biblioteca da Meta, por coleta da página pública (Apify).

A API oficial não devolve anúncio comercial brasileiro. A busca é pelo nome da
página no Brasil, só anúncios ativos; só contam anúncios cuja página tem nome
parecido com o do negócio. Página não achada => "não verificado".
"""

import os
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from radar.cnpj_match import _similar, normalizar_nome
from radar.fontes.apify import rodar_ator
from radar.fontes.base import FonteAnuncios, ResultadoAnuncios

ATOR = "curious_coder/facebook-ads-library-scraper"
DOLAR = float(os.environ.get("RADAR_DOLAR_BRL", "5.04"))
LIMIAR_PAGINA = 0.8


def url_busca(termo: str, pais: str = "BR") -> str:
    return ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            f"&country={pais}&q={quote(termo)}&search_type=keyword_unordered&media_type=all")


def _data(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=timezone.utc).date()
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except (ValueError, OSError):
        return None


def resumir(itens: list[dict], nome: str, pagina: str | None) -> ResultadoAnuncios:
    """Filtra os anúncios da página do negócio e monta o resumo da F6."""
    alvo = normalizar_nome(pagina or nome)
    da_pagina = [i for i in itens
                 if _similar(normalizar_nome(i.get("page_name") or ""), alvo) >= LIMIAR_PAGINA
                 and i.get("is_active", True)]
    if not da_pagina:
        # busca feita e nenhuma página do negócio anunciando => 0 ativos.
        return ResultadoAnuncios(None, pagina, 0, None, None, [])
    pagina_id = str(da_pagina[0].get("page_id") or "") or None
    inicios = [d for d in (_data(i.get("start_date")) for i in da_pagina) if d]
    amostras = []
    for i in da_pagina[:3]:
        snap = i.get("snapshot") or {}
        texto = ((snap.get("body") or {}).get("text") or "").strip()
        amostras.append({"texto": texto[:500],
                         "link_criativo": f"https://www.facebook.com/ads/library/?id={i.get('ad_archive_id')}"})
    link = (f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=BR"
            f"&view_all_page_id={pagina_id}") if pagina_id else None
    return ResultadoAnuncios(pagina_id, da_pagina[0].get("page_name"), len(da_pagina),
                             min(inicios) if inicios else None, link, amostras)


class AnunciosApify(FonteAnuncios):
    chave = "apify_meta_ads"
    rotulo = "Biblioteca de Anúncios da Meta (Apify)"
    env = ("APIFY_TOKEN",)
    custo_unitario_brl = round(0.75 / 1000 * DOLAR, 6)       # por anúncio devolvido

    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None):
        self.token = token or os.environ.get("APIFY_TOKEN", "")
        self.client = client

    async def verificar(self, pagina: str | None, nome: str, pais: str = "BR") -> ResultadoAnuncios:
        itens = await rodar_ator(ATOR, {"urls": [{"url": url_busca(pagina or nome, pais)}], "count": 30},
                                 self.token, self.client)
        self.custo_ultima_chamada = max(self.estimar(len(itens)), round(0.75 / 1000 * DOLAR, 6))
        return resumir(itens, nome, pagina)
