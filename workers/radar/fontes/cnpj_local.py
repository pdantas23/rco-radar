"""F3 · CNPJ pela base própria (radar.cnpj_base, Dados Abertos da Receita): custo zero.

Candidatos por nome + município na tabela local; o detalhe (sócios, Simples/MEI, abertura, capital,
endereço) continua vindo da BrasilAPI, grátis, só para o CNPJ escolhido. A decisão é a mesma do cnpj_match.
"""

import httpx

from radar.cnpj_match import _base, _similar, normalizar_nome
from radar.fontes.base import DadosCnpj, FonteCnpj
from radar.fontes.cnpj import BRASILAPI_URL, TIMEOUT, termos_busca
from radar.importar_cnpj import ALIAS_MUNICIPIO

LIMITE_SQL = 50                 # candidatos que saem do banco, ordenados por semelhança
LIMITE = 10                     # candidatos que vão para o cnpj_match (igual à Casa dos Dados)
MIN_TRIGRAMA = 3                # termo menor que isso não usa o índice

BUSCA = """
select cnpj, razao_social, nome_fantasia, uf, municipio, cnae_principal, porte
from radar.cnpj_base
where uf = %(uf)s and municipio = %(municipio)s
  and (nome_busca like any(%(termos)s) or nome_busca operator(radar.%%) %(nome)s)
order by radar.similarity(nome_busca, %(nome)s) desc
limit %(limite)s
"""


class BaseCnpjSemDados(RuntimeError):
    """A UF ainda não foi importada: o lead fica 'nao_verificado', e não 'nao_encontrado'."""


def _municipio(nome: str) -> str:
    n = " ".join(_base(nome).split())
    return ALIAS_MUNICIPIO.get(n, n)


def converter(linha: tuple) -> DadosCnpj:
    cnpj, razao, fantasia, uf, municipio, cnae, porte = linha
    cnpj = cnpj.strip()
    return DadosCnpj(
        cnpj=cnpj, razao_social=razao, nome_fantasia=fantasia, situacao="ATIVA",   # a base só guarda ativas
        abertura=None, porte=porte, natureza=None, capital_social=None,
        cnae_principal=(cnae or "").strip() or None, cnae_descricao=None, cnaes_secundarios=[], regime=None,
        socios=[], email_rf=None, telefone_rf=None, logradouro="", numero="", municipio=municipio,
        uf=uf.strip(), cep="", eh_filial=cnpj[8:12] != "0001", cnpj_matriz=cnpj[:8] + "0001",
        bruto={"base_local": {"cnpj": cnpj, "razao_social": razao, "nome_fantasia": fantasia}},
    )


class CnpjBaseLocal(FonteCnpj):
    chave = "base_local"
    rotulo = "CNPJ (base própria da Receita + BrasilAPI)"
    env = ()
    custo_unitario_brl = 0.0

    def __init__(self, conn, client: httpx.AsyncClient | None = None):
        self.conn = conn
        self.client = client
        self._ufs_com_dados: set[str] = set()

    def _exigir_uf(self, uf: str) -> None:
        if uf in self._ufs_com_dados:
            return
        if not self.conn.execute("select 1 from radar.cnpj_base where uf = %s limit 1", (uf,)).fetchone():
            raise BaseCnpjSemDados(f"radar.cnpj_base ainda não tem dados de {uf}")
        self._ufs_com_dados.add(uf)

    async def buscar_por_nome(self, nome: str, municipio: str, uf: str) -> list[DadosCnpj]:
        self.custo_ultima_chamada = 0.0
        uf = (uf or "").strip().upper()
        cidade = _municipio(municipio)
        termos = termos_busca(nome)
        if not termos or not uf or not cidade:
            return []
        self._exigir_uf(uf)
        alvo = normalizar_nome(nome)
        padroes = [f"%{t}%" for t in termos if len(t) >= MIN_TRIGRAMA] or [f"%{alvo}%"]
        linhas = self.conn.execute(BUSCA, {"uf": uf, "municipio": cidade, "termos": padroes, "nome": alvo,
                                           "limite": LIMITE_SQL}).fetchall()

        def nota(linha) -> float:
            return max(_similar(alvo, normalizar_nome(linha[2])), _similar(alvo, normalizar_nome(linha[1])))

        return [converter(l) for l in sorted(linhas, key=nota, reverse=True)[:LIMITE]]

    async def detalhar(self, cnpj: str) -> dict | None:
        c = self.client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            r = await c.get(BRASILAPI_URL.format(cnpj=cnpj))
        finally:
            if self.client is None:
                await c.aclose()
        return r.json() if r.status_code == 200 else None
