"""Cliente mínimo da Apify: roda um ator e devolve os itens do dataset.

Usa o endpoint síncrono (run-sync-get-dataset-items). A Apify não informa o
custo na resposta; cada adaptador estima pelo preço por resultado do ator.
"""

import httpx

BASE = "https://api.apify.com/v2/acts"
TIMEOUT = httpx.Timeout(connect=15.0, read=310.0, write=15.0, pool=15.0)


class ErroApify(RuntimeError):
    pass


async def rodar_ator(ator: str, entrada: dict, token: str,
                     client: httpx.AsyncClient | None = None) -> list[dict]:
    """`ator` no formato "dono/nome" (ex.: compass/crawler-google-places)."""
    if not token:
        raise ErroApify("APIFY_TOKEN não configurado")
    url = f"{BASE}/{ator.replace('/', '~')}/run-sync-get-dataset-items"
    proprio = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        resp = await client.post(url, params={"token": token, "clean": "true"}, json=entrada)
    finally:
        if proprio:
            await client.aclose()
    if resp.status_code >= 400:
        raise ErroApify(f"{ator}: HTTP {resp.status_code} {resp.text[:300]}")
    dados = resp.json()
    if not isinstance(dados, list):
        raise ErroApify(f"{ator}: resposta inesperada {str(dados)[:200]}")
    return dados
