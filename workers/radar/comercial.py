"""Cliente das rotas internas do COMERCIAL usadas pelo Radar (Fase 3).

POST {COMERCIAL_URL}/api/internal/radar/enviar  — envia pelo WhatsApp da agência; conversa no inbox.
POST {COMERCIAL_URL}/api/internal/radar/passar  — passa a conversa para um humano (e cria o negócio
                                                   quando o lead é qualificado).
Autenticação: header x-radar-secret = RADAR_INTERNAL_SECRET.
"""

import os
from dataclasses import dataclass

import httpx

TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)


@dataclass
class ResultadoEnvio:
    ok: bool
    conversation_id: str | None = None
    contact_id: str | None = None
    message_id: str | None = None
    motivo: str | None = None


class Comercial:
    def __init__(self, url: str | None = None, segredo: str | None = None, client: httpx.AsyncClient | None = None):
        self.url = (url or os.environ.get("COMERCIAL_URL", "")).rstrip("/")
        self.segredo = segredo or os.environ.get("RADAR_INTERNAL_SECRET", "")
        self.client = client

    async def _post(self, rota: str, corpo: dict) -> dict:
        if not self.url or not self.segredo:
            raise RuntimeError("COMERCIAL_URL / RADAR_INTERNAL_SECRET não configurados")
        c = self.client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            r = await c.post(f"{self.url}{rota}", json=corpo, headers={"x-radar-secret": self.segredo})
        finally:
            if self.client is None:
                await c.aclose()
        if r.status_code >= 500:
            raise RuntimeError(f"COMERCIAL {rota}: HTTP {r.status_code}")
        dados = r.json() if r.content else {}
        if r.status_code >= 400 and "motivo" not in dados:
            raise RuntimeError(f"COMERCIAL {rota}: HTTP {r.status_code} {str(dados)[:200]}")
        return dados

    async def enviar(self, *, agency_id, lead_id, whatsapp_config_id, telefone, nome, texto,
                     primeira: bool, idempotency_key: str | None = None) -> ResultadoEnvio:
        d = await self._post("/api/internal/radar/enviar", {
            "agency_id": agency_id, "lead_id": lead_id, "whatsapp_config_id": whatsapp_config_id,
            "telefone": telefone, "nome": nome, "texto": texto, "primeira": primeira,
            "idempotency_key": idempotency_key})
        return ResultadoEnvio(bool(d.get("ok")), d.get("conversation_id"), d.get("contact_id"),
                              d.get("message_id"), d.get("motivo"))

    async def passar(self, *, agency_id, lead_id, conversation_id, motivo: str,
                     campos: dict | None = None, resumo: str | None = None) -> dict:
        return await self._post("/api/internal/radar/passar", {
            "agency_id": agency_id, "lead_id": lead_id, "conversation_id": conversation_id,
            "motivo": motivo, "campos": campos or {}, "resumo": resumo})
