"""F5 · Instagram via Apify. Só leitura do perfil público: nunca segue, curte ou interage."""

import os
import re
from datetime import datetime, timedelta, timezone

import httpx

from radar.fontes.apify import rodar_ator
from radar.fontes.base import FonteInstagram, PerfilInstagram
from radar.privacidade import limpar

ATOR_PERFIL = "apify/instagram-profile-scraper"
ATOR_BUSCA = "apify/instagram-search-scraper"
DOLAR = float(os.environ.get("RADAR_DOLAR_BRL", "5.04"))
WHATS_BIO = re.compile(r"wa\.me/|whats\s*app|zap|\(\d{2}\)\s*9\d{4}", re.I)


def _data(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


def converter(item: dict, agora: datetime | None = None) -> PerfilInstagram | None:
    usuario = item.get("username")
    if not usuario:
        return None
    agora = agora or datetime.now(timezone.utc)
    datas = sorted((d for d in (_data(p.get("timestamp")) for p in item.get("latestPosts") or []) if d), reverse=True)
    bio = item.get("biography") or ""
    link = item.get("externalUrl") or ""
    return PerfilInstagram(
        usuario=usuario,
        seguidores=item.get("followersCount"),
        seguindo=item.get("followsCount"),
        posts_total=item.get("postsCount"),
        ultimo_post_em=datas[0] if datas else None,
        # amostra de posts recentes: conta só os que caem nos últimos 30 dias
        posts_30d=sum(1 for d in datas if agora - d <= timedelta(days=30)) if datas else 0,
        bio=bio,
        bio_tem_link=bool(link),
        bio_tem_whatsapp=bool(WHATS_BIO.search(bio + " " + link)),
        conta_comercial=item.get("isBusinessAccount"),
        bruto=limpar(item),
    )


class InstagramApify(FonteInstagram):
    chave = "apify_instagram"
    rotulo = "Instagram (Apify)"
    env = ("APIFY_TOKEN",)
    custo_unitario_brl = round(2.30 / 1000 * DOLAR, 5)      # plano Starter, por perfil
    limite_por_hora = 200

    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None):
        self.token = token or os.environ.get("APIFY_TOKEN", "")
        self.client = client

    async def perfil(self, usuario: str) -> PerfilInstagram | None:
        itens = await rodar_ator(ATOR_PERFIL, {"usernames": [usuario.lstrip("@")]}, self.token, self.client)
        self.custo_ultima_chamada = self.estimar(len(itens))
        return next((p for p in (converter(i) for i in itens) if p), None)

    async def procurar(self, nome: str, cidade: str) -> list[str]:
        itens = await rodar_ator(ATOR_BUSCA, {"search": f"{nome} {cidade}", "searchType": "user",
                                              "searchLimit": 3}, self.token, self.client)
        self.custo_ultima_chamada = self.estimar(len(itens))
        return [i["username"] for i in itens if i.get("username")]
