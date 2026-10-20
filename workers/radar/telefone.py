"""Telefone brasileiro: normalização para E.164 e detecção de WhatsApp provável (F2).

Regra do levantamento: celular (9 dígitos, começando com 9) => tem_whatsapp_provavel.
Link wa.me na ficha => número exato de WhatsApp.
"""

import re
from dataclasses import dataclass

# DDDs válidos no Brasil (Anatel).
DDDS = {
    11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38,
    41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68,
    69, 71, 73, 74, 75, 77, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 95,
    96, 97, 98, 99,
}

WA_ME_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send\?phone=|whatsapp\.com/send/\?phone=)\+?(\d{10,13})", re.I)


@dataclass(frozen=True)
class Telefone:
    e164: str          # "+5586999998888"
    ddd: int
    celular: bool


def normalizar(bruto: str | None) -> Telefone | None:
    """Converte qualquer formato comum para E.164. None se não for número BR válido."""
    if not bruto:
        return None
    dig = re.sub(r"\D", "", bruto)
    if dig.startswith("0"):                       # 0 + operadora + DDD (ex.: 0 21 86 ...)
        dig = dig[3:] if len(dig) in (13, 14) else dig[1:]
    if dig.startswith("55") and len(dig) in (12, 13):
        dig = dig[2:]
    if len(dig) not in (10, 11):
        return None
    ddd = int(dig[:2])
    if ddd not in DDDS:
        return None
    assinante = dig[2:]
    if len(assinante) == 9:
        if assinante[0] != "9":
            return None
        celular = True
    else:
        # 8 dígitos: fixo começa com 2-5; celular antigo sem o 9 (6-9) ganha o 9.
        if assinante[0] in "6789":
            assinante = "9" + assinante
            celular = True
        elif assinante[0] in "2345":
            celular = False
        else:
            return None
    return Telefone(e164=f"+55{ddd}{assinante}", ddd=ddd, celular=celular)


def whatsapp_de_link(texto: str | None) -> str | None:
    """Número exato de um link wa.me / api.whatsapp.com, em E.164."""
    if not texto:
        return None
    m = WA_ME_RE.search(texto)
    if not m:
        return None
    tel = normalizar(m.group(1))
    return tel.e164 if tel else None
