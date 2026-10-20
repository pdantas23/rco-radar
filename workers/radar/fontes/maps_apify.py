"""F2 · Google Maps via Apify (ator compass/crawler-google-places).

Reserva: um adaptador Google Places oficial com a mesma interface pode
substituir este sem mexer no resto (regra de arquitetura).
"""

import os
import unicodedata
from datetime import datetime

import httpx

from radar.fontes.apify import rodar_ator
from radar.fontes.base import Avaliacao, ConsultaMaps, FonteMaps, NegocioMaps

ATOR = "compass/crawler-google-places"
DOLAR = float(os.environ.get("RADAR_DOLAR_BRL", "5.04"))

UFS = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA", "ceara": "CE",
    "distrito federal": "DF", "espirito santo": "ES", "goias": "GO", "maranhao": "MA",
    "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG", "para": "PA",
    "paraiba": "PB", "parana": "PR", "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
    "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE", "tocantins": "TO",
}


def uf_de(estado: str | None) -> str:
    """'State of Piauí' / 'Piauí' / 'PI' -> 'PI'. Vazio se não reconhecer."""
    if not estado:
        return ""
    t = unicodedata.normalize("NFKD", estado).encode("ascii", "ignore").decode().lower().strip()
    t = t.removeprefix("state of ").removeprefix("estado de ").removeprefix("estado do ").strip()
    if len(t) == 2 and t.upper() in UFS.values():
        return t.upper()
    return UFS.get(t, "")


def _data(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def converter(item: dict, uf_padrao: str = "") -> NegocioMaps | None:
    """Item do dataset -> NegocioMaps. None se faltar o mínimo (place_id, nome, endereço)."""
    place_id = item.get("placeId")
    nome = (item.get("title") or "").strip()
    endereco = (item.get("address") or "").strip()
    if not (place_id and nome and endereco):
        return None
    loc = item.get("location") or {}
    reviews = sorted(item.get("reviews") or [], key=lambda r: r.get("publishedAtDate") or "", reverse=True)[:5]
    return NegocioMaps(
        place_id=place_id,
        nome=nome,
        categoria=item.get("categoryName") or "",
        endereco=endereco,
        cidade=item.get("city") or "",
        estado=uf_de(item.get("state")) or uf_padrao,
        cep=item.get("postalCode") or "",
        lat=loc.get("lat"),
        lng=loc.get("lng"),
        telefone=item.get("phoneUnformatted") or item.get("phone"),
        site=item.get("website"),
        nota=item.get("totalScore"),
        qtd_avaliacoes=item.get("reviewsCount"),
        horario={h.get("day"): h.get("hours") for h in item.get("openingHours") or [] if h.get("day")} or None,
        link_maps=item.get("url") or f"https://www.google.com/maps/place/?q=place_id:{place_id}",
        fechado_permanentemente=bool(item.get("permanentlyClosed")),
        # sem nome do autor (LGPD): só nota, texto e data
        avaliacoes=[Avaliacao(autor="", nota=int(r.get("stars") or 0), texto=(r.get("text") or "").strip(),
                              data=_data(r.get("publishedAtDate"))) for r in reviews if r.get("text")],
    )


class MapsApify(FonteMaps):
    chave = "apify_maps"
    rotulo = "Google Maps (Apify)"
    env = ("APIFY_TOKEN",)
    # US$ por lugar: base 1,50/1k + detalhes ~1,75/1k + 5 avaliações x 0,40/1k (pesquisa 19/10/2026)
    custo_unitario_brl = round((1.50 + 1.75 + 2.0) / 1000 * DOLAR, 5)

    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None):
        self.token = token or os.environ.get("APIFY_TOKEN", "")
        self.client = client

    def entrada(self, c: ConsultaMaps) -> dict:
        return {
            "searchStringsArray": [c.termo],
            "customGeolocation": {"type": "Point", "coordinates": [c.lng, c.lat], "radiusKm": c.raio_km},
            "maxCrawledPlacesPerSearch": c.limite,
            "language": "pt-BR",
            "countryCode": "br",
            "maxReviews": 5,
            "reviewsSort": "newest",
            "scrapeReviewsPersonalData": False,
            "skipClosedPlaces": True,
            "scrapePlaceDetailPage": True,
        }

    async def buscar(self, consulta: ConsultaMaps, uf_padrao: str = "") -> list[NegocioMaps]:
        itens = await rodar_ator(ATOR, self.entrada(consulta), self.token, self.client)
        negocios = [n for n in (converter(i, uf_padrao) for i in itens) if n]
        self.custo_ultima_chamada = self.estimar(len(itens))
        return negocios
