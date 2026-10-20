"""F2 · Google Maps pelo gosom/google-maps-scraper rodando no nosso servidor (custo zero).

API REST do motor (v1.18.1): POST /api/v1/jobs cria a busca, GET /api/v1/jobs/{id}
devolve o status (pending | working | ok | failed), GET /api/v1/jobs/{id}/download
devolve um CSV. Campos complexos vêm como JSON dentro da célula.

Medido na VPS em 20/10/2026 (docs/gosom-teste/RESUMO.md): ~22 lugares/min por núcleo em
qualquer profundidade; depth 10 traz ~76 lugares por busca em ~3,5 min; 2 núcleos com
-c 2 chegam a ~35 lugares/min. O bug #338 (depth >= 3 falhar) não ocorreu na v1.18.1.
Padrão: depth 10 (GOSOM_DEPTH), limite de ~70 lugares por consulta para a grade.
Se a busca falhar, a reserva (Apify) assume — ver FonteMapsComReserva.
"""

import asyncio
import csv
import io
import json
import math
import os
import re
from datetime import datetime

import httpx

from radar.fontes.base import Avaliacao, ConsultaMaps, FonteMaps, NegocioMaps
from radar.fontes.maps_apify import uf_de
from radar.privacidade import limpar

TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
FECHADO_RE = re.compile(r"permanent", re.I)      # "Permanently closed" / "Fechado permanentemente"


class ErroGosom(RuntimeError):
    pass


def zoom_para_raio(raio_km: float) -> int:
    """Raio da célula -> zoom do mapa (zoom 16 ≈ 1 km de raio; cada nível dobra a área)."""
    return max(10, min(18, round(16 - math.log2(max(raio_km, 0.5)))))


def _json(celula: str):
    try:
        return json.loads(celula) if celula and celula not in ("null", "") else None
    except json.JSONDecodeError:
        return None


def _data(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


def _num(v, tipo=float):
    try:
        return tipo(v) if v not in (None, "", "null") else None
    except ValueError:
        return None


def converter(linha: dict, uf_padrao: str = "") -> NegocioMaps | None:
    place_id = linha.get("place_id") or ""
    nome = (linha.get("title") or "").strip()
    endereco = (linha.get("address") or "").strip()
    if not (place_id and nome and endereco):
        return None
    end = _json(linha.get("complete_address")) or {}
    avaliacoes = []
    revs = sorted(_json(linha.get("user_reviews")) or [], key=lambda r: r.get("published_at") or "", reverse=True)
    for r in revs:
        texto = (r.get("Description") or "").strip()
        if texto and len(avaliacoes) < 5:
            avaliacoes.append(Avaliacao(autor="", nota=int(r.get("Rating") or 0), texto=texto,
                                        data=_data(r.get("published_at"))))
    bruto = {k: (_json(v) if isinstance(v, str) and v[:1] in "[{" else v) for k, v in linha.items()}
    for r in bruto.get("user_reviews") or []:
        r.pop("Name", None)
        r.pop("ProfilePicture", None)
        r.pop("AuthorURL", None)
        r.pop("author_url", None)
    bruto.pop("user_reviews_extended", None)
    horas = _json(linha.get("open_hours"))
    return NegocioMaps(
        place_id=place_id,
        nome=nome,
        categoria=linha.get("category") or "",
        endereco=endereco,
        cidade=end.get("city") or "",
        estado=uf_de(end.get("state")) or uf_padrao,
        cep=end.get("postal_code") or "",
        lat=_num(linha.get("latitude")),
        lng=_num(linha.get("longitude")),
        telefone=linha.get("phone") or None,
        site=linha.get("website") or None,
        nota=_num(linha.get("review_rating")),
        qtd_avaliacoes=_num(linha.get("review_count"), int),
        horario={d: ", ".join(h) for d, h in horas.items()} if isinstance(horas, dict) else None,
        link_maps=linha.get("link") or f"https://www.google.com/maps/place/?q=place_id:{place_id}",
        fechado_permanentemente=bool(FECHADO_RE.search(linha.get("status") or "")),
        avaliacoes=avaliacoes,
        bruto=limpar(bruto),
    )


class MapsGosom(FonteMaps):
    chave = "gosom_maps"
    rotulo = "Google Maps (gosom, servidor próprio)"
    env = ("GOSOM_URL",)
    custo_unitario_brl = 0.0

    def __init__(self, url: str | None = None, client: httpx.AsyncClient | None = None,
                 espera_seg: float = 5.0, tempo_max_seg: int = 900, depth: int | None = None):
        self.url = (url or os.environ.get("GOSOM_URL", "http://127.0.0.1:18080")).rstrip("/")
        self.client = client
        self.espera_seg = espera_seg
        self.tempo_max_seg = tempo_max_seg
        self.depth = depth or int(os.environ.get("GOSOM_DEPTH", "10"))
        # ~7,6 lugares por nível de profundidade (76 em depth 10), com folga
        self.limite_por_consulta = max(16, int(self.depth * 7))

    async def buscar(self, consulta: ConsultaMaps, uf_padrao: str = "") -> list[NegocioMaps]:
        c = self.client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            r = await c.post(f"{self.url}/api/v1/jobs", json={
                "name": f"radar {consulta.termo}", "keywords": [consulta.termo], "lang": "pt",
                "zoom": zoom_para_raio(consulta.raio_km), "lat": f"{consulta.lat:.6f}", "lon": f"{consulta.lng:.6f}",
                "depth": self.depth, "email": False, "max_time": self.tempo_max_seg})
            if r.status_code >= 400:
                raise ErroGosom(f"criar job: HTTP {r.status_code} {r.text[:200]}")
            job_id = r.json()["id"]
            limite = asyncio.get_event_loop().time() + self.tempo_max_seg + 60
            while True:
                # a struct Job do motor não tem tag json: o campo sai como "Status"
                st = (await c.get(f"{self.url}/api/v1/jobs/{job_id}")).json().get("Status")
                if st == "ok":
                    break
                if st == "failed":
                    raise ErroGosom(f"job {job_id} falhou no motor")
                if asyncio.get_event_loop().time() > limite:
                    raise ErroGosom(f"job {job_id} passou do tempo")
                await asyncio.sleep(self.espera_seg)
            d = await c.get(f"{self.url}/api/v1/jobs/{job_id}/download")
            if d.status_code >= 400:
                raise ErroGosom(f"download: HTTP {d.status_code}")
        finally:
            if self.client is None:
                await c.aclose()
        linhas = list(csv.DictReader(io.StringIO(d.text)))
        self.custo_ultima_chamada = 0.0
        return [n for n in (converter(l, uf_padrao) for l in linhas) if n][:consulta.limite]


class FonteMapsComReserva(FonteMaps):
    """Principal (gosom) com reserva (Apify): se a principal falhar, a reserva assume.

    `chave` e `custo_ultima_chamada` refletem a fonte que de fato respondeu,
    para o custo ir certo para custos_api.
    """

    def __init__(self, principal: FonteMaps, reserva: FonteMaps | None):
        self.principal, self.reserva = principal, reserva
        self.chave = principal.chave
        self.limite_por_consulta = getattr(principal, "limite_por_consulta", 60)
        self.ultima_falha: str | None = None
        self.permitir_reserva = True          # a etapa desliga quando a reserva paga não cabe no teto

    def estimar(self, quantidade: int) -> float:
        return self.principal.estimar(quantidade)       # a principal (gosom) é grátis

    async def buscar(self, consulta: ConsultaMaps, uf_padrao: str = "") -> list[NegocioMaps]:
        try:
            r = await self.principal.buscar(consulta, uf_padrao=uf_padrao)
            self.chave, self.custo_ultima_chamada, self.ultima_falha = self.principal.chave, \
                self.principal.custo_ultima_chamada, None
            return r
        except Exception as e:                       # noqa: BLE001 — qualquer falha aciona a reserva
            self.ultima_falha = f"{type(e).__name__}: {e}"
            if self.reserva is None or not self.permitir_reserva:
                raise
        r = await self.reserva.buscar(consulta, uf_padrao=uf_padrao)
        self.chave, self.custo_ultima_chamada = self.reserva.chave, self.reserva.custo_ultima_chamada
        return r
