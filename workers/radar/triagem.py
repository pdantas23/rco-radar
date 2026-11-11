"""Triagem IA: nota determinística e decisão por cortes (contrato: docs/triagem-ia.md).

Mesmo princípio de `qualification.classificar` do analisador-comercial: a IA só devolve FATOS
(radar/ia.py: Fatos); este módulo transforma fatos + respostas da LP + critérios da agência
(radar.criterios_triagem) em nota 0-100, detalhe e decisão. Mesma entrada + mesma versão = mesmo resultado.

Decisão (recalculada a cada resposta do lead):
- nota >= corte_passar                    → 'passar'     (vai para o funil SDR)
- teto  <  corte_descartar                → 'descartar'  (nem com tudo o que falta a nota chegaria ao corte)
- senão                                   → 'acompanhar' (segue a conversa e os acompanhamentos 24 h / +48 h)
`teto` = nota + pesos dos critérios cujo fato ainda é desconhecido. Assim um lead não é descartado só
porque ainda não respondeu às perguntas.
"""

import json
import unicodedata
from dataclasses import dataclass

INDEFINIDOS = {"", "indefinido", "indefinida", "nao_sei", "none", "null"}

# Textos de EXEMPLO até o Miguel definir. A agência sobrescreve em radar.config, chave 'triagem_mensagens',
# com o mesmo formato ({"lp": {"inicial": "..."}, ...}). Variável aceita: {agencia}.
MENSAGENS_PADRAO = {
    "lp": {
        "inicial": "Olá! Aqui é do time da {agencia}. Recebemos seu cadastro e queria entender um pouco melhor "
                   "o seu momento para te direcionar da forma certa. Posso te fazer umas perguntas rápidas?",
        "passar": "Perfeito, obrigado pelas respostas! Vou passar para alguém do time, que já te chama por aqui.",
        "descartar": "Obrigado pelas respostas! Registrei tudo aqui e, se fizer sentido, o time entra em contato.",
    },
    "radar": {
        "passar": "Perfeito, obrigado! Vou passar seu contato para alguém do time, que já te chama por aqui.",
        "descartar": "Obrigado pelo retorno! Registrei aqui e, se fizer sentido no futuro, o time entra em contato.",
    },
}


@dataclass
class Criterios:
    id: int
    versao: int
    criterios: list[dict]
    corte_passar: int
    corte_descartar: int
    exemplo: bool = False


@dataclass
class Resultado:
    nota: int
    teto: int
    decisao: str
    detalhe: dict


def _norm(v) -> str:
    s = unicodedata.normalize("NFKD", str(v)).encode("ascii", "ignore").decode().lower()
    return " ".join(s.split())


def carregar_criterios(conn, agency_id, origem: str) -> Criterios | None:
    row = conn.execute(
        """select id, versao, criterios, corte_passar, corte_descartar, exemplo from radar.criterios_triagem
           where agency_id = %s and origem = %s and ativo""", (agency_id, origem)).fetchone()
    return Criterios(*row) if row else None


def valor_do_campo(campo: str, fatos: dict, respostas: list[dict]):
    """Fato da IA pelo nome, ou resposta da LP com `resposta:<trecho da pergunta>`. None = desconhecido."""
    if campo.startswith("resposta:"):
        alvo = _norm(campo.split(":", 1)[1])
        for r in respostas or []:
            if alvo and alvo in _norm(r.get("pergunta") or ""):
                v = r.get("resposta")
                return None if v is None or _norm(v) in INDEFINIDOS else v
        return None
    v = (fatos or {}).get(campo)
    return None if v is None or _norm(v) in INDEFINIDOS else v


def atende(regra: dict, valor) -> bool:
    tipo = regra.get("tipo")
    if tipo == "preenchido":
        return True
    valores = [_norm(x) for x in regra.get("valores") or []]
    if tipo == "em":
        return _norm(valor) in valores
    if tipo == "contem":
        return any(x and x in _norm(valor) for x in valores)
    raise ValueError(f"regra de triagem desconhecida: {tipo!r}")


def decidir(nota: int, teto: int, corte_passar: int, corte_descartar: int) -> str:
    if nota >= corte_passar:
        return "passar"
    if teto < corte_descartar:
        return "descartar"
    return "acompanhar"


def calcular(crit: Criterios, fatos: dict, respostas: list[dict]) -> Resultado:
    total = sum(int(c["peso"]) for c in crit.criterios) or 1
    pontos = pendentes = 0
    itens = []
    for c in crit.criterios:
        peso = int(c["peso"])
        valor = valor_do_campo(c["campo"], fatos, respostas)
        if valor is None:
            ok = None
            pendentes += peso
        else:
            ok = atende(c["regra"], valor)
            pontos += peso if ok else 0
        itens.append({"campo": c["campo"], "peso": peso, "valor": valor, "atende": ok,
                      "pontos": peso if ok else 0})
    nota = round(pontos * 100 / total)
    teto = round((pontos + pendentes) * 100 / total)
    decisao = decidir(nota, teto, crit.corte_passar, crit.corte_descartar)
    detalhe = {"versao": crit.versao, "exemplo": crit.exemplo, "nota": nota, "teto": teto,
               "corte_passar": crit.corte_passar, "corte_descartar": crit.corte_descartar,
               "decisao": decisao, "criterios": itens, "fatos": fatos}
    return Resultado(nota, teto, decisao, detalhe)


def juntar_fatos(anteriores: dict | None, novos: dict | None) -> dict:
    """Fato conhecido não volta a ser desconhecido; um fato novo conhecido substitui o anterior."""
    fatos = dict(anteriores or {})
    for k, v in (novos or {}).items():
        if v is not None and _norm(v) not in INDEFINIDOS:
            fatos[k] = v
    return fatos


def mensagem(conn, agency_id, origem: str, tipo: str, agencia: str) -> str | None:
    """Texto configurado (radar.config 'triagem_mensagens') ou o de exemplo; None se não houver."""
    row = conn.execute("select valor from radar.config where agency_id = %s and chave = 'triagem_mensagens'",
                       (agency_id,)).fetchone()
    cfg = row[0] if row else {}
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    texto = ((cfg or {}).get(origem) or {}).get(tipo) or MENSAGENS_PADRAO.get(origem, {}).get(tipo)
    return texto.replace("{agencia}", agencia) if texto else None


def ficha_lp(contexto: dict) -> str:
    respostas = (contexto or {}).get("respostas") or []
    linhas = ["A pessoa preencheu o formulário da landing page da agência e pediu contato."]
    if respostas:
        linhas.append("Respostas do formulário (já respondidas, NÃO pergunte de novo):")
        linhas += [f"- {r.get('pergunta')}: {r.get('resposta')}" for r in respostas]
    return "\n".join(linhas)
