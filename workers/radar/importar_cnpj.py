"""Importa a base de CNPJ dos Dados Abertos da Receita Federal para radar.cnpj_base.

Rotina completa em docs/cnpj-base-local.md. Uso:
    python -m radar.importar_cnpj --uf PI                  # última competência, baixando em streaming
    python -m radar.importar_cnpj --uf PI --dir /tmp/rf    # arquivos .zip já baixados
    python -m radar.importar_cnpj                          # Brasil inteiro

Layout conferido em 09/11/2026 no cnpj-metadados.pdf e em amostra real da competência 2026-10:
- Repositório (Nextcloud do SERPRO, compartilhamento público):
  https://arquivos.receitafederal.gov.br/index.php/s/YggdBLfdninEJX9, pasta AAAA-MM.
- Estabelecimentos{0..9}.zip: 30 colunas, separador ";", latin-1, tudo entre aspas.
- Empresas{0..9}.zip: 7 colunas. A razão social e o porte só existem aqui.
- Municipios.zip: "código";"nome". O código é o da Receita (TOM, 4 dígitos), NÃO o do IBGE.

Memória limitada: arquivos lidos em fluxo, e as empresas são filtradas por um bitmap dos CNPJs
básicos que interessam (12,5 MB, cabe o Brasil inteiro). O cruzamento fica a cargo do Postgres.

Idempotente: tudo numa transação só, em tabelas temporárias. Rodar de novo não duplica. Linha
inalterada não é regravada; CNPJ que deixou de estar ativo sai da base. Arquivo truncado aborta
tudo (CRC e fim do fluxo são conferidos) e nada muda.

LGPD: não lê o arquivo de Sócios, e tira o CPF que aparece no nome de MEI/empresário individual.
"""

import argparse
import csv
import logging
import os
import re
import sys
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from radar.cnpj_match import _base, normalizar_nome

log = logging.getLogger("radar.importar_cnpj")

BASE_URL = "https://arquivos.receitafederal.gov.br/public.php/webdav"
TOKEN = "YggdBLfdninEJX9"           # token público do compartilhamento, não é segredo
PARTES = range(10)
COLUNAS_ESTAB, COLUNAS_EMPRESA = 30, 7
ATIVA = {"02", "2"}
PORTE = {"01": "ME", "03": "EPP", "05": "DEMAIS"}       # 00 = não informado
UFS = {"AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE",
       "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"}
CNPJ_RE = re.compile(r"^[0-9A-Z]{12}[0-9]{2}$")
CPF_RE = re.compile(r"(?<![\d.\-/])\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?![\d.\-/])")
TAM_BLOCO = 1 << 20
TOLERANCIA_RUINS = 0.001            # mais que 0,1% de linhas ruins: o layout mudou, aborta
QUEDA_MAXIMA = 0.5                  # não apaga a base se a carga nova vier com menos da metade

# Nomes em que a tabela da Receita difere da do IBGE (que é a que o Maps usa).
# Levantado comparando Municipios.zip (2026-10) com a API de localidades do IBGE.
ALIAS_MUNICIPIO = {
    "amparo da serra": "amparo do serra", "amparo de sao francisco": "amparo do sao francisco",
    "ares": "arez", "balneario de picarras": "balneario picarras", "boa saude": "januario cicco",
    "brasopolis": "brazopolis", "couto de magalhaes": "couto magalhaes",
    "eldorado dos carajas": "eldorado do carajas", "fortaleza do tabocao": "tabocao",
    "muquem de sao francisco": "muquem do sao francisco", "parati": "paraty",
    "santa isabel do para": "santa izabel do para", "santana do livramento": "sant ana do livramento",
    "santo antonio do leverger": "santo antonio de leverger", "sao luiz": "sao luis",
    "sao valerio da natividade": "sao valerio",
}


class ArquivoInvalido(RuntimeError):
    """Arquivo truncado, corrompido ou com layout diferente do esperado: a importação aborta."""


# ── leitura em fluxo ─────────────────────────────────────────────────────
def inflar_zip(blocos: Iterable[bytes]) -> Iterator[bytes]:
    """Descompacta em fluxo o primeiro (e único) membro de um .zip, sem precisar de seek."""
    it, buf = iter(blocos), bytearray()

    def garantir(n: int) -> None:
        while len(buf) < n:
            try:
                buf.extend(next(it))
            except StopIteration:
                raise ArquivoInvalido("zip truncado no cabeçalho") from None

    garantir(30)
    if bytes(buf[:4]) != b"PK\x03\x04":
        raise ArquivoInvalido("não é um arquivo zip")
    flags = int.from_bytes(buf[6:8], "little")
    metodo = int.from_bytes(buf[8:10], "little")
    crc_esperado = int.from_bytes(buf[14:18], "little")
    fim = 30 + int.from_bytes(buf[26:28], "little") + int.from_bytes(buf[28:30], "little")
    if metodo != 8:
        raise ArquivoInvalido(f"método de compressão {metodo} não suportado")
    garantir(fim)
    z, crc, pendente = zlib.decompressobj(-15), 0, bytes(buf[fim:])
    while True:
        try:
            saida = z.decompress(pendente)
        except zlib.error as e:
            raise ArquivoInvalido(f"zip corrompido: {e}") from e
        if saida:
            crc = zlib.crc32(saida, crc)
            yield saida
        if z.eof:
            break
        try:
            pendente = next(it)
        except StopIteration:
            raise ArquivoInvalido("zip truncado: o arquivo terminou antes da hora") from None
    if not flags & 0x08 and crc != crc_esperado:
        raise ArquivoInvalido("zip corrompido: CRC não confere")


def linhas(blocos: Iterable[bytes]) -> Iterator[str]:
    """Latin-1 byte a byte, sem NUL (a Receita publica alguns) e sem acumular o arquivo."""
    resto = ""
    for b in blocos:
        texto = resto + b.decode("latin-1").replace("\x00", "")
        partes = texto.split("\n")
        resto = partes.pop()
        yield from partes
    if resto.strip():
        yield resto


def campos(linha: str) -> list[str] | None:
    """Divide por '";"' e não usa csv por padrão: aspas soltas no meio de um nome não engolem as linhas seguintes."""
    linha = linha.rstrip("\r")
    if len(linha) >= 2 and linha[0] == '"' and linha[-1] == '"':
        return [c.replace('""', '"') for c in linha[1:-1].split('";"')]
    try:
        return next(csv.reader([linha], delimiter=";"))
    except (csv.Error, StopIteration):
        return None


def sem_cpf(texto: str) -> str:
    """'MARIA SILVA 12345678901' -> 'MARIA SILVA'. CNPJ (14 dígitos) e números curtos ficam."""
    return " ".join(CPF_RE.sub(" ", texto or "").split())


def normalizar_municipio(nome: str) -> str:
    n = " ".join(_base(nome).split())
    return ALIAS_MUNICIPIO.get(n, n)


def normalizar_cep(cep: str) -> str | None:
    d = re.sub(r"\D", "", cep or "")
    return d if len(d) == 8 and int(d) else None


def nome_busca(fantasia: str, razao: str) -> str:
    f, r = normalizar_nome(fantasia), normalizar_nome(razao)
    return f"{f} {r}".strip() if f != r else r


# ── conversão de cada linha ──────────────────────────────────────────────
@dataclass
class Contagem:
    lidas: int = 0
    ruins: int = 0
    aproveitadas: int = 0

    def conferir(self, nome: str) -> None:
        if self.lidas and self.ruins / self.lidas > TOLERANCIA_RUINS:
            raise ArquivoInvalido(f"{nome}: {self.ruins} de {self.lidas} linhas fora do layout")


@dataclass
class Bitmap:
    """Conjunto de CNPJs básicos. Numéricos num bitmap fixo; os alfanuméricos (poucos) num set."""
    bits: bytearray = field(default_factory=lambda: bytearray(12_500_000))
    alfa: set = field(default_factory=set)

    def add(self, basico: str) -> None:
        if basico.isdigit():
            n = int(basico)
            self.bits[n >> 3] |= 1 << (n & 7)
        else:
            self.alfa.add(basico)

    def __contains__(self, basico: str) -> bool:
        if basico.isdigit():
            n = int(basico)
            return bool(self.bits[n >> 3] & (1 << (n & 7)))
        return basico in self.alfa


def estabelecimento(c: list[str], municipios: dict[str, str], ufs: set[str] | None) -> tuple | None:
    """Linha de Estabelecimentos -> (cnpj, basico, fantasia, uf, municipio, cnae, logradouro, numero, cep), ou None."""
    if c[5].strip() not in ATIVA:
        return None
    uf = c[19].strip().upper()
    if uf not in UFS or (ufs and uf not in ufs):
        return None
    cnpj = (c[0] + c[1] + c[2]).strip().upper()
    if not CNPJ_RE.match(cnpj):
        raise ValueError("cnpj fora do formato")
    municipio = municipios.get(c[20].strip())
    if not municipio:
        raise ValueError("município desconhecido")
    # logradouro com o tipo na frente, igual à montagem usada para a Casa dos Dados
    logradouro = sem_cpf(" ".join(x for x in (c[13].strip(), c[14].strip()) if x))
    return cnpj, c[0].strip(), sem_cpf(c[4]), uf, municipio, c[11].strip()[:7] or None, logradouro, sem_cpf(c[15]), \
        normalizar_cep(c[18])


def empresa(c: list[str]) -> tuple[str, str, str | None]:
    """Linha de Empresas -> (basico, razão social sem CPF, porte)."""
    return c[0].strip(), sem_cpf(c[1]), PORTE.get(c[5].strip())


def carregar_municipios(blocos: Iterable[bytes]) -> dict[str, str]:
    saida = {}
    for linha in linhas(inflar_zip(blocos)):
        c = campos(linha)
        if c and len(c) == 2:
            saida[c[0].strip()] = normalizar_municipio(c[1])
    if len(saida) < 5000:
        raise ArquivoInvalido(f"Municipios: só {len(saida)} linhas")
    return saida


# ── origem dos arquivos ──────────────────────────────────────────────────
class Origem:
    def __init__(self, competencia: str | None = None, pasta: Path | None = None,
                 client: httpx.Client | None = None):
        self.pasta = pasta
        self.client = client or httpx.Client(auth=(TOKEN, ""), follow_redirects=True,
                                             timeout=httpx.Timeout(connect=30, read=120, write=30, pool=30))
        self.competencia = competencia or (None if pasta else self.ultima_competencia())

    def ultima_competencia(self) -> str:
        r = self.client.request("PROPFIND", f"{BASE_URL}/", headers={"Depth": "1"})
        r.raise_for_status()
        meses = sorted(set(re.findall(r"/webdav/(\d{4}-\d{2})/", r.text)))
        if not meses:
            raise ArquivoInvalido("nenhuma competência publicada")
        return meses[-1]

    def blocos(self, nome: str) -> Iterator[bytes]:
        if self.pasta:
            with open(self.pasta / nome, "rb") as f:
                while b := f.read(TAM_BLOCO):
                    yield b
            return
        with self.client.stream("GET", f"{BASE_URL}/{self.competencia}/{nome}") as r:
            r.raise_for_status()
            yield from r.iter_bytes(TAM_BLOCO)


# ── carga no banco ───────────────────────────────────────────────────────
TEMPORARIAS = """
create temp table _estab (cnpj text, basico text, nome_fantasia text, uf text, municipio text, cnae text,
                          logradouro text, numero text, cep text) on commit drop;
create temp table _empresa (basico text, razao_social text, porte text) on commit drop;
"""

MONTAR_FINAL = """
create temp table _final on commit drop as
select distinct on (e.cnpj) e.cnpj, m.razao_social, e.nome_fantasia, e.uf, e.municipio, e.cnae, m.porte,
       e.logradouro, e.numero, e.cep
from _estab e join _empresa m using (basico)
order by e.cnpj
"""

GRAVAR = """
insert into radar.cnpj_base as b (cnpj, razao_social, nome_fantasia, uf, municipio, cnae_principal, porte,
                                  logradouro, numero, cep, nome_busca)
select cnpj, razao_social, nome_fantasia, uf, municipio, cnae, porte, logradouro, numero, cep, nome_busca from _final
on conflict (cnpj) do update set
  razao_social = excluded.razao_social, nome_fantasia = excluded.nome_fantasia, uf = excluded.uf,
  municipio = excluded.municipio, cnae_principal = excluded.cnae_principal, porte = excluded.porte,
  logradouro = excluded.logradouro, numero = excluded.numero, cep = excluded.cep, nome_busca = excluded.nome_busca
where (b.razao_social, b.nome_fantasia, b.uf, b.municipio, b.cnae_principal, b.porte, b.logradouro, b.numero, b.cep,
       b.nome_busca)
      is distinct from
      (excluded.razao_social, excluded.nome_fantasia, excluded.uf, excluded.municipio, excluded.cnae_principal,
       excluded.porte, excluded.logradouro, excluded.numero, excluded.cep, excluded.nome_busca)
"""


def importar(conn, origem: Origem, ufs: set[str] | None = None, *, recriar_indice: bool = False,
             forcar: bool = False) -> dict:
    """Carrega tudo numa transação. Qualquer exceção desfaz a importação inteira."""
    municipios = carregar_municipios(origem.blocos("Municipios.zip"))
    basicos, ce, cm = Bitmap(), Contagem(), Contagem()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(TEMPORARIAS)
        with cur.copy("copy _estab from stdin") as cp:
            for i in PARTES:
                for linha in linhas(inflar_zip(origem.blocos(f"Estabelecimentos{i}.zip"))):
                    ce.lidas += 1
                    c = campos(linha)
                    if not c or len(c) != COLUNAS_ESTAB:
                        ce.ruins += 1
                        continue
                    try:
                        reg = estabelecimento(c, municipios, ufs)
                    except ValueError:
                        ce.ruins += 1
                        continue
                    if reg:
                        ce.aproveitadas += 1
                        basicos.add(reg[1])
                        cp.write_row(reg)
                ce.conferir(f"Estabelecimentos{i}")
                log.info("Estabelecimentos%d: %d lidas, %d aproveitadas", i, ce.lidas, ce.aproveitadas)
        with cur.copy("copy _empresa from stdin") as cp:
            for i in PARTES:
                for linha in linhas(inflar_zip(origem.blocos(f"Empresas{i}.zip"))):
                    cm.lidas += 1
                    c = campos(linha)
                    if not c or len(c) != COLUNAS_EMPRESA:
                        cm.ruins += 1
                        continue
                    reg = empresa(c)
                    if reg[0] in basicos:
                        cm.aproveitadas += 1
                        cp.write_row(reg)
                cm.conferir(f"Empresas{i}")
                log.info("Empresas%d: %d lidas, %d aproveitadas", i, cm.lidas, cm.aproveitadas)
        cur.execute("analyze _estab"); cur.execute("analyze _empresa")
        cur.execute(MONTAR_FINAL)
        cur.execute("alter table _final add column nome_busca text")
        _preencher_nome_busca(conn, cur)
        total = cur.execute("select count(*) from _final").fetchone()[0]
        filtro = "b.uf = any(%s)" if ufs else "true"
        args = (sorted(ufs),) if ufs else ()
        atual = cur.execute(f"select count(*) from radar.cnpj_base b where {filtro}", args).fetchone()[0]
        if total == 0 or (atual and total < atual * QUEDA_MAXIMA and not forcar):
            raise ArquivoInvalido(f"carga suspeita: {total} linhas novas contra {atual} atuais (use --forcar se for real)")
        cur.execute("create index on _final (cnpj)")
        if recriar_indice:
            cur.execute("drop index radar.cnpj_base_nome_trgm_idx")
        cur.execute(GRAVAR)
        gravadas = cur.rowcount
        cur.execute(f"delete from radar.cnpj_base b where {filtro} "
                    "and not exists (select 1 from _final f where f.cnpj = b.cnpj)", args)
        apagadas = cur.rowcount
        if recriar_indice:
            cur.execute("set local maintenance_work_mem = '1GB'")
            cur.execute("create index cnpj_base_nome_trgm_idx on radar.cnpj_base using gin (nome_busca radar.gin_trgm_ops)")
    return {"estabelecimentos": ce, "empresas": cm, "total": total, "gravadas": gravadas, "apagadas": apagadas}


def _preencher_nome_busca(conn, cur) -> None:
    """Calculado em Python para usar exatamente a normalização do cnpj_match (sem unaccent no banco)."""
    cur.execute("create temp table _nomes (cnpj text primary key, nome_busca text) on commit drop")
    # a leitura e o COPY não podem estar abertos juntos na mesma conexão: alterna em lotes
    with conn.cursor(name="ler_final") as ler:
        ler.execute("select cnpj, nome_fantasia, razao_social from _final")
        while lote := ler.fetchmany(20_000):
            with cur.copy("copy _nomes from stdin") as cp:
                for cnpj, fantasia, razao in lote:
                    cp.write_row((cnpj, nome_busca(fantasia, razao)))
    cur.execute("update _final f set nome_busca = n.nome_busca from _nomes n where n.cnpj = f.cnpj")


def main(argv: list[str] | None = None) -> int:
    import psycopg

    p = argparse.ArgumentParser(description="Importa CNPJs ativos dos Dados Abertos da Receita para radar.cnpj_base")
    p.add_argument("--uf", action="append", help="limita a um estado (pode repetir); sem isso, o Brasil inteiro")
    p.add_argument("--competencia", help="AAAA-MM; padrão: a última publicada")
    p.add_argument("--dir", type=Path, help="pasta com os .zip já baixados (em vez de baixar em streaming)")
    p.add_argument("--dsn-env", default="RADAR_DATABASE_URL", help="variável de ambiente com a conexão")
    p.add_argument("--recriar-indice", action="store_true", help="carga inicial grande: refaz o índice de nome no fim")
    p.add_argument("--forcar", action="store_true", help="aceita carga muito menor que a base atual")
    a = p.parse_args(argv)
    ufs = {u.upper() for u in a.uf} if a.uf else None
    if ufs and ufs - UFS:
        p.error(f"UF inválida: {sorted(ufs - UFS)}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dsn = os.environ.get(a.dsn_env)
    if not dsn:
        p.error(f"variável {a.dsn_env} não definida")
    origem = Origem(a.competencia, a.dir)
    log.info("competência %s, UFs %s", origem.competencia or "(pasta local)", sorted(ufs) if ufs else "todas")
    with psycopg.connect(dsn) as conn:
        r = importar(conn, origem, ufs, recriar_indice=a.recriar_indice, forcar=a.forcar)
    log.info("fim: %s", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
