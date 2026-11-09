"""Modo só dados abertos (RADAR_SO_DADOS_ABERTOS=1): garante custo zero.

Sem Apify (Maps, Instagram, anúncios), sem Casa dos Dados, sem IA e sem WhatsApp. Ficam gosom, base
própria de CNPJ, BrasilAPI, leitura do site e Nominatim, todos grátis.

A garantia tem três camadas independentes:
1. worker.fontes_reais nem cria os adaptadores pagos nesse modo.
2. Cada ponto de saída paga chama exigir_liberado() antes de abrir conexão (apify.rodar_ator,
   CnpjCasaDosDados._post_casa, ia.responder, Comercial._post). Código que escape da camada 1 falha aqui.
3. etapas.custo recusa gravar qualquer valor maior que zero.
"""

import os

from radar.fontes.base import Adaptador

VERDADEIRO = {"1", "true", "sim", "on"}


class FontePagaBloqueada(RuntimeError):
    """Chamada paga barrada pelo modo só dados abertos. Não adianta tentar de novo: a falha é definitiva."""


def ativo() -> bool:
    return os.environ.get("RADAR_SO_DADOS_ABERTOS", "").strip().lower() in VERDADEIRO


def exigir_liberado(fonte: str) -> None:
    if ativo():
        raise FontePagaBloqueada(f"modo só dados abertos: chamada paga barrada ({fonte})")


class FonteBloqueada(Adaptador):
    """No lugar de cada fonte paga. Qualquer busca falha antes de sair da máquina."""

    chave = "bloqueada_custo_zero"
    rotulo = "Fonte paga (bloqueada no modo só dados abertos)"
    custo_unitario_brl = 0.0
    habilitada = False

    def __init__(self, nome: str):
        self.nome = nome

    def _barrar(self, *_a, **_k):
        raise FontePagaBloqueada(f"modo só dados abertos: chamada paga barrada ({self.nome})")

    async def buscar(self, *a, **k):
        self._barrar()

    async def procurar(self, *a, **k):
        self._barrar()

    async def perfil(self, *a, **k):
        self._barrar()

    async def verificar(self, *a, **k):
        self._barrar()
