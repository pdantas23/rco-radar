"""F3 · CNPJ: candidatos pela Casa dos Dados (busca por nome + município),
detalhes pela BrasilAPI (grátis, CNPJ exato: CNAE, Simples/MEI, sócios).

Sócios: só nome e qualificação. Documento/CPF nunca é lido para o modelo.
"""

import os
import re
import unicodedata
from datetime import date

import httpx

from radar.cnpj_match import RUIDO_NOME
from radar.fontes.base import DadosCnpj, FonteCnpj
from radar.privacidade import limpar

CASA_URL = "https://api.casadosdados.com.br/v5/cnpj/pesquisa"
BRASILAPI_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

PORTE = {"01": "ME", "03": "EPP", "05": "DEMAIS",
         "MICRO EMPRESA": "ME", "EMPRESA DE PEQUENO PORTE": "EPP", "DEMAIS": "DEMAIS"}


def _sem_acento(t: str) -> str:
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()


def termos_busca(nome: str) -> list[str]:
    """'Barbearia do Zé' -> ['barbearia', 'ze']: palavras que distinguem o negócio."""
    palavras = re.sub(r"[^a-z0-9 ]+", " ", _sem_acento(nome)).split()
    return [p for p in palavras if p not in RUIDO_NOME and len(p) > 1][:4]


def _data(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _tel(ddd_tel: str | None) -> str | None:
    dig = re.sub(r"\D", "", ddd_tel or "")
    return dig or None


def converter_casa(item: dict) -> DadosCnpj:
    end = item.get("endereco") or {}
    porte = (item.get("porte_empresa") or {}).get("codigo") or ""
    socios = [{"nome": s.get("nome"), "qualificacao": s.get("qualificacao_socio")}
              for s in item.get("quadro_societario") or []]
    logradouro = " ".join(x for x in (end.get("tipo_logradouro"), end.get("logradouro")) if x)
    return DadosCnpj(
        cnpj=re.sub(r"\D", "", item.get("cnpj") or ""),
        razao_social=item.get("razao_social") or "",
        nome_fantasia=item.get("nome_fantasia") or "",
        situacao=((item.get("situacao_cadastral") or {}).get("situacao_cadastral") or "").upper(),
        abertura=_data(item.get("data_abertura")),
        porte=PORTE.get(porte),
        natureza=item.get("descricao_natureza_juridica"),
        capital_social=item.get("capital_social"),
        cnae_principal=None, cnae_descricao=None, cnaes_secundarios=[], regime=None,
        socios=socios, email_rf=None, telefone_rf=None,
        logradouro=logradouro, numero=str(end.get("numero") or ""),
        municipio=end.get("municipio") or "", uf=(end.get("uf") or "").upper(), cep=end.get("cep") or "",
        eh_filial=(item.get("matriz_filial") or "").upper() == "FILIAL",
        cnpj_matriz=(item.get("cnpj_raiz") or "") + "0001",
        bruto={"casa": limpar(item)},
    )


def completar_brasilapi(d: DadosCnpj, item: dict) -> DadosCnpj:
    """Preenche CNAE, regime, contato e sócios com o retorno da BrasilAPI."""
    d.cnae_principal = str(item.get("cnae_fiscal") or "") or None
    d.cnae_descricao = item.get("cnae_fiscal_descricao")
    d.cnaes_secundarios = [{"codigo": c.get("codigo"), "descricao": c.get("descricao")}
                           for c in item.get("cnaes_secundarios") or [] if c.get("codigo")]
    if item.get("opcao_pelo_mei"):
        d.regime, d.porte = "MEI", "MEI"
    elif item.get("opcao_pelo_simples"):
        d.regime = "Simples"
    else:
        d.regime = "Outro"
    d.email_rf = item.get("email") or None
    d.telefone_rf = _tel(item.get("ddd_telefone_1"))
    if item.get("qsa"):
        d.socios = [{"nome": s.get("nome_socio"), "qualificacao": s.get("qualificacao_socio")}
                    for s in item["qsa"]]
    d.porte = d.porte or PORTE.get((item.get("porte") or "").upper())
    d.bruto["brasilapi"] = limpar(item)
    return d


class CnpjCasaDosDados(FonteCnpj):
    chave = "casadosdados"
    rotulo = "CNPJ (Casa dos Dados + BrasilAPI)"
    env = ("CASADOSDADOS_API_KEY",)
    custo_unitario_brl = 0.01          # por CNPJ devolvido (a confirmar na contratação)

    def __init__(self, api_key: str | None = None, client: httpx.AsyncClient | None = None):
        self.api_key = api_key or os.environ.get("CASADOSDADOS_API_KEY", "")
        self.client = client

    async def _post_casa(self, corpo: dict) -> list[dict]:
        c = self.client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            r = await c.post(CASA_URL, params={"tipo_resultado": "completo"}, json=corpo,
                             headers={"api-key": self.api_key})
        finally:
            if self.client is None:
                await c.aclose()
        r.raise_for_status()
        return (r.json() or {}).get("cnpjs") or []

    async def detalhar(self, cnpj: str) -> dict | None:
        c = self.client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            r = await c.get(BRASILAPI_URL.format(cnpj=cnpj))
        finally:
            if self.client is None:
                await c.aclose()
        return r.json() if r.status_code == 200 else None

    async def buscar_por_nome(self, nome: str, municipio: str, uf: str) -> list[DadosCnpj]:
        termos = termos_busca(nome)
        if not termos:
            self.custo_ultima_chamada = 0.0
            return []
        corpo = {
            "busca_textual": [{"texto": termos, "tipo_busca": "radical",
                               "razao_social": True, "nome_fantasia": True, "nome_socio": False}],
            "uf": [uf.lower()],
            "municipio": [_sem_acento(municipio)],
            "limite": 10,
            "pagina": 1,
        }
        itens = await self._post_casa(corpo)
        self.custo_ultima_chamada = self.estimar(len(itens))
        return [converter_casa(i) for i in itens]
