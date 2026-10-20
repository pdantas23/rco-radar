"""Decifra as chaves gravadas pelo COMERCIAL (AES-256-GCM, mesmo esquema de src/lib/whatsapp/encryption.ts).

Formato: "<iv_hex>:<ciphertext_hex>:<authtag_hex>"; iv de 12 bytes, tag de 16; sem associated data.
Chave mestra: ENCRYPTION_KEY (64 hex), a MESMA do app COMERCIAL — vem do ambiente, nunca do banco.
"""

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def decifrar(valor: str | None, chave_hex: str | None = None) -> str | None:
    if not valor:
        return None
    chave_hex = chave_hex or os.environ.get("ENCRYPTION_KEY", "")
    if len(chave_hex) != 64:
        raise RuntimeError("ENCRYPTION_KEY ausente ou inválida (precisa de 64 caracteres hex)")
    partes = valor.split(":")
    if len(partes) != 3:
        raise ValueError("chave cifrada em formato desconhecido (esperado iv:ct:tag)")
    iv, ct, tag = (bytes.fromhex(p) for p in partes)
    return AESGCM(bytes.fromhex(chave_hex)).decrypt(iv, ct + tag, None).decode("utf-8")


def cifrar(texto: str, chave_hex: str) -> str:
    """Só para testes: produz o mesmo formato do COMERCIAL."""
    iv = os.urandom(12)
    saida = AESGCM(bytes.fromhex(chave_hex)).encrypt(iv, texto.encode("utf-8"), None)
    return f"{iv.hex()}:{saida[:-16].hex()}:{saida[-16:].hex()}"
