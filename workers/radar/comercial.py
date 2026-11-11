"""Cliente das rotas internas do COMERCIAL usadas pelo Radar (Fase 3).

POST {COMERCIAL_URL}/api/internal/radar/enviar  — envia pelo WhatsApp da agência; conversa no inbox.
POST {COMERCIAL_URL}/api/internal/radar/passar  — passa a conversa para um humano (e cria o negócio
                                                   quando o lead é qualificado).
POST {COMERCIAL_URL}/api/internal/radar/descartar — triagem de LP decidiu 'descartar': encerra a conversa e
                                                   etiqueta o contato com "Fora do perfil".
Origem 'lp' (triagem IA, sem radar.leads): lead_id = None e a conversa é identificada por conversa_id
(radar.conversas.id) no /enviar; o telefone é opcional (o COMERCIAL usa o do contato).
Autenticação: header x-radar-secret = RADAR_INTERNAL_SECRET.
"""

import json
import os
from dataclasses import dataclass

import httpx

from radar import custo_zero

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
        custo_zero.exigir_liberado(f"WhatsApp/COMERCIAL {rota}")
        if not self.url or not self.segredo:
            raise RuntimeError("COMERCIAL_URL / RADAR_INTERNAL_SECRET não configurados")
        c = self.client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            # ids vêm do banco como uuid.UUID: json= do httpx não serializa UUID (TypeError antes de sair)
            r = await c.post(f"{self.url}{rota}", content=json.dumps(corpo, default=str),
                             headers={"x-radar-secret": self.segredo, "content-type": "application/json"})
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
                     primeira: bool, idempotency_key: str | None = None, conversa_id=None) -> ResultadoEnvio:
        d = await self._post("/api/internal/radar/enviar", {
            "agency_id": agency_id, "lead_id": lead_id, "conversa_id": conversa_id,
            "whatsapp_config_id": whatsapp_config_id,
            "telefone": telefone, "nome": nome, "texto": texto, "primeira": primeira,
            "idempotency_key": idempotency_key})
        return ResultadoEnvio(bool(d.get("ok")), d.get("conversation_id"), d.get("contact_id"),
                              d.get("message_id"), d.get("motivo"))

    async def passar(self, *, agency_id, lead_id, conversation_id, motivo: str,
                     campos: dict | None = None, resumo: str | None = None, conversa_id=None) -> dict:
        """LP com 1ª mensagem falha: motivo 'falha_primeira_mensagem', conversation_id None e conversa_id."""
        corpo = {"agency_id": agency_id, "lead_id": lead_id, "conversation_id": conversation_id,
                 "motivo": motivo, "campos": campos or {}, "resumo": resumo}
        if conversa_id is not None:
            corpo["conversa_id"] = conversa_id
        return await self._post("/api/internal/radar/passar", corpo)

    async def descartar(self, *, agency_id, conversa_id, motivo: str | None = None) -> dict:
        return await self._post("/api/internal/radar/descartar", {
            "agency_id": agency_id, "conversa_id": conversa_id, "motivo": motivo})
