"""Casamento ficha do Maps × CNPJ (F3).

Confiança de 0 a 100 com o motivo legível ("nome fantasia e endereço iguais").
Abaixo de 50 o CNPJ não é assumido. Mais de um candidato acima de 50 com
diferença pequena => revisão humana com a lista.
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

LIMIAR_ACEITE = 50
MARGEM_EMPATE = 10        # dois candidatos a menos de 10 pontos => humano decide

# Palavras que não distinguem um negócio de outro.
RUIDO_NOME = {
    "ltda", "me", "epp", "eireli", "mei", "sa", "s/a", "cia", "comercio", "servicos",
    "de", "da", "do", "das", "dos", "e", "a", "o", "the",
}
ABREV_ENDERECO = {
    "r": "rua", "av": "avenida", "trav": "travessa", "tv": "travessa", "al": "alameda",
    "pc": "praca", "pca": "praca", "rod": "rodovia", "est": "estrada", "q": "quadra",
}


def _base(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", t)


def normalizar_nome(nome: str) -> str:
    return " ".join(p for p in _base(nome).split() if p not in RUIDO_NOME)


def normalizar_endereco(end: str) -> tuple[str, str]:
    """(logradouro normalizado, número) — o resto do endereço é ruído para casar."""
    partes = _base(end).split()
    partes = [ABREV_ENDERECO.get(p, p) for p in partes]
    numero = next((p for p in partes if p.isdigit()), "")
    logradouro = []
    for p in partes:
        if p.isdigit():
            break
        logradouro.append(p)
    return " ".join(logradouro), numero


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb)
    return max(jaccard, SequenceMatcher(None, a, b).ratio())


@dataclass
class Negocio:
    nome: str
    endereco: str
    cidade: str
    cep: str = ""
    telefone: str = ""          # E.164


@dataclass
class Candidato:
    cnpj: str
    razao_social: str
    nome_fantasia: str
    logradouro: str
    numero: str
    municipio: str
    cep: str = ""
    telefone: str = ""          # E.164
    situacao: str = ""


@dataclass
class Resultado:
    candidato: Candidato
    confianca: int
    motivo: str


def pontuar(negocio: Negocio, cand: Candidato) -> Resultado:
    """Soma evidências; cada uma vira um trecho do motivo exibido na ficha."""
    if _base(negocio.cidade).strip() != _base(cand.municipio).strip():
        return Resultado(cand, 0, "cidade diferente")

    pontos, motivos = 20, ["mesma cidade"]
    nome_maps = normalizar_nome(negocio.nome)
    sim_fantasia = _similar(nome_maps, normalizar_nome(cand.nome_fantasia))
    sim_razao = _similar(nome_maps, normalizar_nome(cand.razao_social))
    sim_nome = max(sim_fantasia, sim_razao)
    if sim_nome >= 0.95:
        pontos += 40
        motivos.append("nome fantasia igual" if sim_fantasia >= sim_razao else "razão social igual")
    elif sim_nome >= 0.75:
        pontos += 25
        motivos.append("nome parecido")

    log_m, num_m = normalizar_endereco(negocio.endereco)
    log_c = normalizar_endereco(cand.logradouro)[0]
    if _similar(log_m, log_c) >= 0.85:
        if num_m and num_m == re.sub(r"\D", "", cand.numero or ""):
            pontos += 30
            motivos.append("endereço igual")
        else:
            pontos += 15
            motivos.append("mesma rua")

    cep_m, cep_c = re.sub(r"\D", "", negocio.cep), re.sub(r"\D", "", cand.cep)
    if cep_m and cep_m == cep_c:
        pontos += 5
        motivos.append("CEP igual")
    if negocio.telefone and negocio.telefone == cand.telefone:
        pontos += 20
        motivos.append("telefone igual")

    situacao = (cand.situacao or "").strip().upper()
    if situacao and situacao != "ATIVA":
        pontos -= 15
        motivos.append(f"situação {situacao.lower()}")

    return Resultado(cand, max(0, min(100, pontos)), ", ".join(motivos))


@dataclass
class Decisao:
    escolhido: Resultado | None       # None => cnpj_status 'nao_encontrado' ou 'revisao'
    revisao_humana: bool
    candidatos: list[Resultado]       # todos, ordenados, para guardar


def decidir(negocio: Negocio, candidatos: list[Candidato]) -> Decisao:
    res = sorted((pontuar(negocio, c) for c in candidatos), key=lambda r: r.confianca, reverse=True)
    aceitos = [r for r in res if r.confianca >= LIMIAR_ACEITE]
    if not aceitos:
        return Decisao(None, False, res)
    if len(aceitos) > 1 and aceitos[0].confianca - aceitos[1].confianca < MARGEM_EMPATE:
        return Decisao(None, True, res)
    return Decisao(aceitos[0], False, res)


def texto_ficha(r: Resultado) -> str:
    """'CNPJ casado com confiança 85%: nome fantasia igual, endereço igual'."""
    return f"CNPJ casado com confiança {r.confianca}%: {r.motivo}"
