"""Triagem IA v2: portões determinísticos + 4 saídas (contrato: docs/triagem-ia.md, seção v2).

A IA só devolve FATOS (radar/ia.py: Fatos); este módulo transforma fatos + regras da agência
(radar.config 'triagem_regras', padrões em REGRAS_PADRAO) em classificação e justificativa.
Funções puras: mesma entrada = mesmo resultado.

Saídas (avaliar):
- desqualifica  — algum eliminatório (marketing visto como despesa, sem orçamento nem intenção, pede algo fora)
                  aparece: decide na hora; ou, com tudo conhecido, algum portão falhou além do porte;
- redireciona   — com tudo conhecido, o ÚNICO portão em falha é o porte (faturamento): candidato a gestão de
                  tráfego isolada (passa ao SDR com alerta; porte mínimo dela A DEFINIR);
- esteira       — sem falha, mas timing > 60 dias ou "só pesquisando";
- passa_direto  — tudo ok;
- passa_alerta  — algum portão em alerta (o contrato fala em "exatamente um"; com mais de um também passa com
                  todos os alertas no resumo — decisão conservadora, o SDR decide);
- em_andamento  — falta fato: a conversa segue. Decisor indefinido / não decisor sem contato só seguram a decisão
                  enquanto falta outro fato; "no fim" viram alerta (contrato).
"""

import copy
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

INDEFINIDOS = {"", "indefinido", "indefinida", "nao_sei", "none", "null"}

# ── regras (radar.config 'triagem_regras') ───────────────────────────────
REGRAS_PADRAO = {
    "origens": ["lp"],                                   # onde a v2 vale; 'radar' entra quando a agência incluir
    "faturamento": {"passa_min": 60000, "alerta_min": 50000},
    "cidades": {"opala": ["Teresina"], "proximas": ["Timon"]},
    "timing": {"passa": ["ate_30d", "30_60d"], "esteira": ["mais_60d", "so_pesquisando"]},
    "trafego_isolado_porte_min": None,                   # A DEFINIR (cliente); None = redireciona sempre
}

FAIXAS = {"ate_30k": (0, 30000), "30_60k": (30000, 60000), "60_100k": (60000, 100000),
          "acima_100k": (100000, float("inf"))}
ROTULO_FAIXA = {"ate_30k": "até R$ 30 mil", "30_60k": "R$ 30 a 60 mil", "60_100k": "R$ 60 a 100 mil",
                "acima_100k": "acima de R$ 100 mil"}


def _norm(v) -> str:
    s = unicodedata.normalize("NFKD", str(v)).encode("ascii", "ignore").decode().lower()
    return " ".join(s.split())


def conhecido(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, tuple)):
        return len(v) > 0
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v > 0
    return _norm(v) not in INDEFINIDOS


def _config(conn, agency_id, chave):
    row = conn.execute("select valor from radar.config where agency_id = %s and chave = %s",
                       (agency_id, chave)).fetchone()
    v = row[0] if row else None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = None
    return v if isinstance(v, dict) else {}


def _mesclar(base: dict, extra: dict) -> dict:
    """Config da agência por cima do padrão, só onde o tipo bate (valor inválido = padrão)."""
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if k not in out:
            continue
        if isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _mesclar(out[k], v)
        elif out[k] is None and (v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)):
            out[k] = v
        elif isinstance(out[k], list) and isinstance(v, list) and all(isinstance(x, str) for x in v):
            out[k] = v
        elif isinstance(out[k], (int, float)) and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = v
    return out


def regras(conn, agency_id) -> dict:
    r = _mesclar(REGRAS_PADRAO, _config(conn, agency_id, "triagem_regras"))
    if r["faturamento"]["alerta_min"] > r["faturamento"]["passa_min"]:
        r["faturamento"] = dict(REGRAS_PADRAO["faturamento"])
    return r


def v2_ativa(regras_ag: dict, origem: str) -> bool:
    return origem in (regras_ag.get("origens") or [])


# ── portões ──────────────────────────────────────────────────────────────
@dataclass
class Portao:
    portao: str
    status: str                 # ok | alerta | falha | esteira | pendente
    valor: object
    motivo: str

    def dict(self):
        return {"portao": self.portao, "status": self.status, "valor": self.valor, "motivo": self.motivo}


def _mil(v: float) -> str:
    return f"R$ {v / 1000:g} mil".replace(".", ",")


def faixa_ambigua(fatos: dict, r: dict) -> bool:
    """A faixa cruza um corte (ex.: 30-60k com cortes 50k/60k) e não há valor nem refino: pede UMA pergunta de refino."""
    if conhecido(fatos.get("faturamento_valor")) or not conhecido(fatos.get("faixa_faturamento")):
        return False
    lo, hi = FAIXAS.get(fatos["faixa_faturamento"], (None, None))
    if lo is None:
        return False
    f = r["faturamento"]
    return _status_intervalo(lo, hi, f) is None and not conhecido(fatos.get("faixa_refino"))


def _status_intervalo(lo, hi, f):
    if lo >= f["passa_min"]:
        return "ok"
    if hi <= f["alerta_min"]:
        return "falha"
    if lo >= f["alerta_min"] and hi <= f["passa_min"]:
        return "alerta"
    return None


def portao_faturamento(fatos: dict, r: dict) -> Portao:
    f = r["faturamento"]
    valor = fatos.get("faturamento_valor")
    if conhecido(valor):
        v = float(valor)
        st = "ok" if v >= f["passa_min"] else "alerta" if v >= f["alerta_min"] else "falha"
        return Portao("faturamento", st, v, _motivo_fat(st, _mil(v), f))
    faixa = fatos.get("faixa_faturamento")
    if not conhecido(faixa) or faixa not in FAIXAS:
        return Portao("faturamento", "pendente", None, "faixa de faturamento ainda não informada")
    lo, hi = FAIXAS[faixa]
    st = _status_intervalo(lo, hi, f)
    rotulo = ROTULO_FAIXA[faixa]
    if st is None:
        refino = fatos.get("faixa_refino")
        if refino == "perto_do_maior":
            st, rotulo = ("alerta" if hi <= f["passa_min"] else "ok"), rotulo + " (mais perto do topo)"
        elif refino == "perto_do_menor":
            st, rotulo = ("falha" if lo < f["alerta_min"] else "alerta"), rotulo + " (mais perto da base)"
        else:
            return Portao("faturamento", "pendente", faixa, f"faixa {rotulo} cruza o corte: falta a pergunta de refino")
    return Portao("faturamento", st, faixa, _motivo_fat(st, rotulo, f))


def _motivo_fat(st, rotulo, f):
    if st == "ok":
        return f"faturamento {rotulo} (>= {_mil(f['passa_min'])})"
    if st == "alerta":
        return f"faturamento {rotulo} (entre {_mil(f['alerta_min'])} e {_mil(f['passa_min'])})"
    return f"faturamento {rotulo} (abaixo de {_mil(f['alerta_min'])}: porte)"


def portao_decisor(fatos: dict) -> Portao:
    d = fatos.get("decisor")
    if d == "dono_socio":
        return Portao("decisor", "ok", d, "é dono/sócio (decide)")
    if d == "participa":
        return Portao("decisor", "alerta", d, "participa da decisão, mas não decide sozinho")
    if d == "nao_decide":
        contato = fatos.get("contato_decisor")
        if conhecido(contato):
            return Portao("decisor", "alerta", d, f"não é o decisor; contato do decisor: {contato}")
        return Portao("decisor", "pendente", d, "não é o decisor: pedir o contato do decisor ou conversa com os dois")
    return Portao("decisor", "pendente", None, "ainda não se sabe quem decide")


def portao_cidade(fatos: dict, r: dict) -> Portao:
    cidade = fatos.get("cidade")
    if not conhecido(cidade):
        return Portao("cidade", "pendente", None, "cidade ainda não informada")
    n = _norm(cidade)

    def bate(lista):
        return any(_norm(x) and (n == _norm(x) or n.startswith(_norm(x) + " ") or n.startswith(_norm(x) + "-")
                                 or n.startswith(_norm(x) + ","))
                   for x in lista)
    if bate(r["cidades"]["opala"]):
        return Portao("cidade", "ok", cidade, f"{cidade}: atendida pelo Opala")
    if bate(r["cidades"]["proximas"]):
        return Portao("cidade", "alerta", cidade, f"{cidade}: avaliação comercial (cidade próxima)")
    return Portao("cidade", "alerta", cidade, f"{cidade}: ticket diferenciado (fora da região do Opala)")


def portao_necessidade(fatos: dict) -> Portao:
    v = fatos.get("necessidade_clara")
    if v == "sim":
        return Portao("necessidade", "ok", v, "necessidade clara: " + str(fatos.get("dor") or "sim"))
    if v == "nao":
        return Portao("necessidade", "falha", v, "sem necessidade clara")
    return Portao("necessidade", "pendente", None, "necessidade ainda não está clara")


def portao_timing(fatos: dict, r: dict) -> Portao:
    v = fatos.get("prazo_inicio")
    if v in r["timing"]["passa"]:
        return Portao("timing", "ok", v, f"quer começar: {v}")
    if v in r["timing"]["esteira"]:
        return Portao("timing", "esteira", v, f"quer começar: {v} (mais de 60 dias ou só pesquisando)")
    return Portao("timing", "pendente", None, "prazo para começar ainda não informado")


ELIMINATORIOS = (
    ("ve_marketing_como_despesa", "sim", "vê marketing como despesa"),
    ("orcamento_ou_intencao", "nao", "sem orçamento nem intenção de investir"),
    ("pede_algo_fora", "sim", "pede algo fora do que a agência faz"),
)


def portoes(fatos: dict, r: dict, conversa_ativa: bool = True) -> list[Portao]:
    lista = [portao_faturamento(fatos, r), portao_decisor(fatos), portao_cidade(fatos, r),
             portao_necessidade(fatos), portao_timing(fatos, r),
             Portao("telefone", "ok" if conversa_ativa else "falha", conversa_ativa,
                    "conversa ativa no WhatsApp" if conversa_ativa else "sem conversa ativa no WhatsApp")]
    for campo, ruim, motivo in ELIMINATORIOS:
        v = fatos.get(campo)
        lista.append(Portao(campo, "falha" if v == ruim else "ok", v, motivo if v == ruim else "não apareceu"))
    return lista


# ── decisão ──────────────────────────────────────────────────────────────
@dataclass
class Resultado:
    classificacao: str
    justificativa: str
    portoes: list[Portao]
    alertas: list[str] = field(default_factory=list)
    falta: list[str] = field(default_factory=list)

    def detalhe(self, fatos: dict) -> dict:
        return {"versao": 2, "classificacao": self.classificacao, "justificativa": self.justificativa,
                "alertas": self.alertas, "falta": self.falta, "portoes": [p.dict() for p in self.portoes],
                "fatos": fatos}


def _abaixo_do_isolado(p: Portao, r: dict) -> bool:
    minimo = r.get("trafego_isolado_porte_min")
    if not minimo:
        return False
    if isinstance(p.valor, (int, float)):
        return p.valor < minimo
    return FAIXAS.get(p.valor, (0, float("inf")))[1] <= minimo


def avaliar(fatos: dict, r: dict, conversa_ativa: bool = True) -> Resultado:
    ps = portoes(fatos or {}, r, conversa_ativa)
    nomes_elim = {c for c, _, _ in ELIMINATORIOS} | {"telefone"}
    elim = [p for p in ps if p.portao in nomes_elim and p.status == "falha"]
    if elim:
        return Resultado("desqualifica", "eliminatório: " + "; ".join(p.motivo for p in elim), ps)

    pendentes = [p for p in ps if p.status == "pendente"]
    if [p for p in pendentes if p.portao != "decisor"]:
        return Resultado("em_andamento", "falta: " + "; ".join(p.motivo for p in pendentes), ps,
                         falta=[p.portao for p in pendentes])
    for p in pendentes:                                   # só o decisor: "no fim" vira alerta (não descarta)
        p.status = "alerta"
        p.motivo += " (no fim da conversa)"

    falhas = [p for p in ps if p.status == "falha"]
    if falhas:
        if [p.portao for p in falhas] == ["faturamento"]:
            if _abaixo_do_isolado(falhas[0], r):
                return Resultado("desqualifica", "abaixo do porte mínimo da gestão de tráfego isolada: "
                                 + falhas[0].motivo, ps)
            alerta = "tráfego isolado: " + falhas[0].motivo
            return Resultado("redireciona", "falhou só no porte do Opala → candidato a gestão de tráfego isolada ("
                             + falhas[0].motivo + ")", ps, alertas=[alerta])
        return Resultado("desqualifica", "falhou em: " + "; ".join(p.motivo for p in falhas), ps)

    timing = next(p for p in ps if p.portao == "timing")
    if timing.status == "esteira":
        return Resultado("esteira", "bom perfil, mas " + timing.motivo, ps)

    alertas = [p.motivo for p in ps if p.status == "alerta"]
    if alertas:
        return Resultado("passa_alerta", "passou com alerta em: " + "; ".join(alertas), ps, alertas=alertas)
    oks = [p.motivo for p in ps if p.portao in ("faturamento", "decisor", "cidade", "necessidade", "timing")]
    return Resultado("passa_direto", "todos os portões ok: " + "; ".join(oks), ps)


def bom_perfil(res: Resultado) -> bool:
    """Para o fim da cadência: porte conhecido e ok/alerta, nenhuma falha (vai à esteira em vez de encerrar)."""
    st = {p.portao: p.status for p in res.portoes}
    return st.get("faturamento") in ("ok", "alerta") and not any(p.status == "falha" for p in res.portoes)


def juntar_fatos(anteriores: dict | None, novos: dict | None) -> dict:
    """Fato conhecido não volta a ser desconhecido; um fato novo conhecido substitui o anterior; listas somam."""
    fatos = dict(anteriores or {})
    for k, v in (novos or {}).items():
        if not conhecido(v):
            continue
        if isinstance(v, list):
            fatos[k] = list(dict.fromkeys([*(fatos.get(k) or []), *v]))
        else:
            fatos[k] = v
    return fatos


# ── roteiro (S, S, N, A, T, B; cidade cedo; extra: experiência com agência) ─
ROTEIRO = (  # (etapa, fatos que a fecham)
    ("situacao", ("segmento",)),
    ("cidade", ("cidade",)),
    ("marketing", ("investe_anuncio",)),
    ("necessidade", ("necessidade_clara",)),
    ("experiencia_agencia", ("experiencia_agencia",)),
    ("autoridade", ("decisor",)),
    ("contato_decisor", ("contato_decisor",)),           # só quando não é o decisor
    ("timing", ("prazo_inicio",)),
    ("faturamento", ("faixa_faturamento",)),             # B por último, em faixas
    ("refino", ("faixa_refino",)),                       # só quando a faixa cruza o corte (30-60k)
)

# Textos de referência (EXEMPLO até a Maria/Miguel fecharem os scripts). A agência sobrescreve em
# radar.config 'triagem_roteiro' = {"<etapa>": "texto"}.
ROTEIRO_PADRAO = {
    "situacao": "Me conta um pouco do seu negócio: qual é o segmento e há quanto tempo vocês estão no mercado?",
    "cidade": "E vocês atendem em qual cidade?",
    "marketing": "Hoje, como os clientes chegam até vocês? Vocês investem em anúncio?",
    "necessidade": "O que mais trava o crescimento hoje? O que você gostaria de resolver primeiro?",
    "experiencia_agencia": "Você já trabalhou com alguma agência ou gestor de tráfego? Como foi a experiência?",
    "autoridade": "E as decisões sobre o marketing aí ficam com você ou com mais alguém?",
    "contato_decisor": "Faz sentido incluirmos essa pessoa: você me passa o contato dela ou prefere que a conversa "
                       "seja com vocês dois?",
    "timing": "Se fizer sentido, quando você pensa em começar?",
    "faturamento": "Para eu te direcionar certo: em qual faixa está o faturamento mensal hoje? Até 30 mil, de 30 a "
                   "60 mil, de 60 a 100 mil ou acima de 100 mil?",
    "refino": "E está mais perto dos 30 ou dos 60 mil?",
}

# Trechos (sem acento, minúsculos) da pergunta do formulário que indicam que o assunto já foi respondido.
# Lista fixa por ora (não vem da config).
TEMAS_FORMULARIO = {
    "situacao": ("segmento", "ramo", "nicho", "tipo de negocio", "area de atuacao"),
    "cidade": ("cidade",),
    "marketing": ("anuncio", "trafego pago", "como os clientes"),
    "necessidade": ("objetivo", "desafio", "dificuldade", "o que precisa"),
    "experiencia_agencia": ("agencia",),
    "autoridade": ("decisor", "decide", "cargo", "dono", "socio"),
    "timing": ("quando pretende", "quando quer", "prazo", "comecar"),
    "faturamento": ("faturamento", "fatura"),
}


def temas_do_formulario(respostas: list[dict]) -> dict[str, str]:
    """{etapa: 'pergunta: resposta'} para cada assunto já respondido no formulário da LP."""
    out: dict[str, str] = {}
    for r in respostas or []:
        if not conhecido(r.get("resposta")):
            continue
        p = _norm(r.get("pergunta") or "")
        for etapa, trechos in TEMAS_FORMULARIO.items():
            if etapa not in out and any(t in p for t in trechos):
                out[etapa] = f"{r.get('pergunta')}: {r.get('resposta')}"
    return out


def _etapa_aberta(etapa: str, fatos: dict, r: dict) -> bool:
    if etapa == "contato_decisor":
        return fatos.get("decisor") == "nao_decide" and not conhecido(fatos.get("contato_decisor"))
    if etapa == "refino":
        return faixa_ambigua(fatos, r)
    if etapa == "faturamento":
        return not (conhecido(fatos.get("faixa_faturamento")) or conhecido(fatos.get("faturamento_valor")))
    campos = dict(ROTEIRO)[etapa]
    return not all(conhecido(fatos.get(c)) for c in campos)


def proxima_etapa(fatos: dict, r: dict, cobertos: dict | None = None) -> tuple[str | None, bool]:
    """(etapa, aprofundar). Primeiro o que nem o formulário respondeu, na ordem do roteiro; depois o que o
    formulário respondeu mas ainda não virou fato (aí a IA aprofunda em vez de perguntar de novo)."""
    cobertos = cobertos or {}
    abertas = [e for e, _ in ROTEIRO if _etapa_aberta(e, fatos, r)]
    for e in abertas:
        if e not in cobertos:
            return e, False
    return (abertas[0], True) if abertas else (None, False)


def textos(conn, agency_id, chave: str, padrao: dict) -> dict:
    cfg = _config(conn, agency_id, chave)
    return {k: (cfg.get(k) if isinstance(cfg.get(k), str) and cfg.get(k).strip() else v) for k, v in padrao.items()}


ROTULO_FATO = {
    "segmento": "segmento", "tempo_mercado": "tempo de mercado", "como_atrai_clientes": "como atrai clientes",
    "investe_anuncio": "investe em anúncio", "valor_anuncio": "quanto investe em anúncio", "dor": "dor/objetivo",
    "necessidade_clara": "necessidade clara", "decisor": "decisor", "contato_decisor": "contato do decisor",
    "prazo_inicio": "quando quer começar", "faixa_faturamento": "faixa de faturamento",
    "faturamento_valor": "faturamento informado", "faixa_refino": "refino da faixa", "cidade": "cidade",
    "experiencia_agencia": "experiência com agência", "ve_marketing_como_despesa": "vê marketing como despesa",
    "orcamento_ou_intencao": "tem orçamento ou intenção", "pede_algo_fora": "pede algo fora do escopo",
    "objecoes": "objeções", "assuntos_comerciais": "perguntou sobre",
}


def tarefa(fatos: dict, r: dict, roteiro: dict, cobertos: dict, base: str) -> str:
    """Instrução da vez para a IA: o próximo assunto do roteiro, o que já se sabe e o que veio do formulário."""
    partes = [base]
    conhecidos = [f"{ROTULO_FATO.get(k, k)}: {v}" for k, v in (fatos or {}).items()
                  if conhecido(v) and not k.endswith("_anterior")]
    if conhecidos:
        partes.append("Já se sabe: " + "; ".join(conhecidos) + ".")
    if cobertos:
        partes.append("Já respondido no formulário (NÃO pergunte de novo; use para aprofundar): "
                      + "; ".join(cobertos.values()) + ".")
    etapa, aprofundar = proxima_etapa(fatos, r, cobertos)
    if etapa:
        if aprofundar:
            partes.append(f"Próximo assunto: {etapa}. O formulário já respondeu ({cobertos[etapa]}): não repita a "
                          "pergunta; use a resposta para aprofundar (ex.: \"Vi que ... Há quanto tempo...\").")
        else:
            partes.append(f"Próximo assunto: {etapa}. Referência: \"{roteiro.get(etapa, '')}\"")
    return " ".join(partes)


# ── cadência e janela (seg-sex 9h-18h no fuso da agência) ────────────────
CADENCIA_PADRAO = {
    # horas a partir do toque 1 (imediato); tipo guia o texto
    "toques": [{"apos_horas": 0, "tipo": "inicial"}, {"apos_horas": 2, "tipo": "retoma"},
               {"apos_horas": 24, "tipo": "pergunta_facil"}, {"apos_horas": 72, "tipo": "valor"},
               {"apos_horas": 168, "tipo": "fechamento"}],
    "encerrar_apos_horas": 48,                # depois do 5º toque sem resposta: esteira (bom perfil) ou encerra
    "janela": {"dias": [0, 1, 2, 3, 4], "inicio": "09:00", "fim": "18:00"},
    "esteira": {"intervalo_dias": 30, "mensagens": 3, "espera_final_dias": 7},
}
INSTRUCOES_TOQUE = {  # EXEMPLO; a agência sobrescreve em radar.config 'triagem_cadencia'.tipos
    "retoma": "O lead ainda não respondeu. Retome a conversa de forma leve, sem cobrar resposta, e refaça a "
              "última pergunta de outro jeito.",
    "pergunta_facil": "O lead ainda não respondeu. Faça UMA pergunta fácil de responder (sim/não ou escolha de "
                      "faixa) sobre o próximo assunto.",
    "valor": "O lead ainda não respondeu. Mande algo de valor ligado ao segmento dele ({segmento}): uma ideia ou "
             "observação curta e útil. Sem cobrar resposta.",
    "fechamento": "O lead ainda não respondeu. Fechamento gentil: diga que não vai mais insistir e deixe a porta "
                  "aberta para quando fizer sentido.",
    "esteira": "Mensagem de acompanhamento da esteira: mande algo de valor (um caso, uma ideia ou uma novidade) "
               "ligado ao segmento do lead ({segmento}). Nunca pergunte se ainda tem interesse.",
}


def _hora(s, padrao: time) -> time:
    try:
        return time.fromisoformat(s)
    except (TypeError, ValueError):
        return padrao


def cadencia(conn, agency_id) -> dict:
    cfg = _config(conn, agency_id, "triagem_cadencia")
    c = copy.deepcopy(CADENCIA_PADRAO)
    toques = cfg.get("toques")
    if (isinstance(toques, list) and 1 <= len(toques) <= 10
            and all(isinstance(t, dict) and isinstance(t.get("apos_horas"), (int, float))
                    and not isinstance(t.get("apos_horas"), bool) and isinstance(t.get("tipo"), str) for t in toques)
            and [t["apos_horas"] for t in toques] == sorted(t["apos_horas"] for t in toques)
            and toques[0]["apos_horas"] == 0):
        c["toques"] = toques
    if isinstance(cfg.get("encerrar_apos_horas"), (int, float)) and cfg["encerrar_apos_horas"] > 0:
        c["encerrar_apos_horas"] = cfg["encerrar_apos_horas"]
    j = cfg.get("janela") or {}
    if isinstance(j, dict):
        dias = j.get("dias")
        if isinstance(dias, list) and dias and all(isinstance(d, int) and 0 <= d <= 6 for d in dias):
            c["janela"]["dias"] = sorted(set(dias))
        ini, fim = _hora(j.get("inicio"), None), _hora(j.get("fim"), None)
        if ini and fim and ini < fim:
            c["janela"]["inicio"], c["janela"]["fim"] = j["inicio"], j["fim"]
    if isinstance(cfg.get("esteira"), dict):
        c["esteira"] = _mesclar(c["esteira"], cfg["esteira"])
    tipos = cfg.get("tipos") if isinstance(cfg.get("tipos"), dict) else {}
    c["tipos"] = {**INSTRUCOES_TOQUE, **{k: v for k, v in tipos.items() if isinstance(v, str) and v.strip()}}
    return c


def na_janela(agora: datetime, fuso: str, janela: dict) -> bool:
    local = agora.astimezone(ZoneInfo(fuso))
    return (local.weekday() in janela["dias"]
            and _hora(janela["inicio"], time(9)) <= local.time() < _hora(janela["fim"], time(18)))


def proxima_janela(quando: datetime, fuso: str, janela: dict) -> datetime:
    """`quando` se estiver dentro da janela; senão, a próxima abertura (dia útil configurado, hora de início)."""
    if na_janela(quando, fuso, janela):
        return quando
    tz = ZoneInfo(fuso)
    local = quando.astimezone(tz)
    ini = _hora(janela["inicio"], time(9))
    dia = local.date()
    if not (local.weekday() in janela["dias"] and local.time() < ini):
        dia += timedelta(days=1)
    while dia.weekday() not in janela["dias"]:
        dia += timedelta(days=1)
    return datetime.combine(dia, ini, tzinfo=tz).astimezone(quando.tzinfo)


def vencimento_toque(c: dict, k: int, inicio: datetime, ultimo: datetime, fuso: str) -> datetime:
    """Quando sai o toque de índice k (0 = toque 1). Conta a partir do toque 1, mas nunca encurta o espaço entre
    dois toques quando o anterior foi empurrado pela janela; o resultado cai sempre dentro da janela."""
    horas = [t["apos_horas"] for t in c["toques"]]
    alvo = inicio + timedelta(hours=horas[k])
    if k > 0:
        alvo = max(alvo, ultimo + timedelta(hours=horas[k] - horas[k - 1]))
    return proxima_janela(alvo, fuso, c["janela"])


# ── o que a IA não trata (código garante; o prompt pede) ─────────────────
ASSUNTO_PROIBIDO_RE = re.compile(
    r"(custa|pre[cç]o|mensalidade|piso|valor (do|da|de) (servi[cç]o|plano|pacote|consultoria|opala|m[eé]todo|investimento)"
    r"|investimento (m[ií]nimo|de|é|fica)|a partir de|fica em|sai por|por m[eê]s)[^.?!]{0,25}r\$\s*\d"
    r"|\d+\s*%\s*(de\s+)?desconto|desconto de\s*\d"
    r"|melhor (que|do que) (a|o) (concorr|ag[eê]ncia)",
    re.I)


def assunto_proibido(texto: str) -> bool:
    """Preço/piso com valor, desconto com número, comparação com concorrente: a mensagem não sai."""
    return bool(ASSUNTO_PROIBIDO_RE.search(texto or ""))


# ── mensagens fixas por tipo (radar.config 'triagem_mensagens') ──────────
# EXEMPLO até o Miguel/Maria definirem. Formato: {"lp": {"inicial": "..."}, "radar": {...}}. Variáveis:
# {agencia}; na oferta {opcao_1} e {opcao_2}; na confirmação {horario}.
MENSAGENS_PADRAO = {
    "lp": {
        "inicial": "Olá! Aqui é do time da {agencia}. Recebemos seu cadastro e queria entender um pouco melhor "
                   "o seu momento para te direcionar da forma certa. Posso te fazer umas perguntas rápidas?",
        "passar": "Perfeito, obrigado pelas respostas! Vou passar para alguém do time, que já te chama por aqui.",
        "descartar": "Obrigado pelas respostas! Registrei tudo aqui e, se fizer sentido, o time entra em contato.",
        "oferta": "Perfeito, obrigado pelas respostas! Faz sentido você conversar com nosso consultor. Tenho estes "
                  "horários: {opcao_1} ou {opcao_2}. Qual fica melhor para você?",
        "confirmacao": "Combinado! Ficou marcado para {horario}. O consultor te encontra nesse horário; qualquer "
                       "imprevisto, é só me avisar por aqui.",
        "oferta_nova": "Poxa, esse horário acabou de ser preenchido. Tenho estes outros: {opcao_1} ou {opcao_2}. "
                       "Qual fica melhor?",
        "esteira": "Entendi, faz sentido esperar o momento certo. Vou te mandar umas ideias de vez em quando e, "
                   "quando quiser retomar, é só me chamar por aqui.",
    },
    "radar": {
        "passar": "Perfeito, obrigado! Vou passar seu contato para alguém do time, que já te chama por aqui.",
        "descartar": "Obrigado pelo retorno! Registrei aqui e, se fizer sentido no futuro, o time entra em contato.",
    },
}


def mensagem(conn, agency_id, origem: str, tipo: str, agencia: str, **vars) -> str | None:
    """Texto configurado ou o de exemplo (radar cai no da LP para os tipos novos); None se não houver."""
    cfg = _config(conn, agency_id, "triagem_mensagens")
    texto = (((cfg or {}).get(origem) or {}).get(tipo) or MENSAGENS_PADRAO.get(origem, {}).get(tipo)
             or MENSAGENS_PADRAO["lp"].get(tipo))
    if not texto:
        return None
    for k, v in {"agencia": agencia, **vars}.items():
        texto = texto.replace("{" + k + "}", str(v))
    return texto


def ficha_lp(contexto: dict) -> str:
    respostas = (contexto or {}).get("respostas") or []
    linhas = ["A pessoa preencheu o formulário da landing page da agência e pediu contato."]
    if respostas:
        linhas.append("Respostas do formulário (já respondidas, NÃO pergunte de novo; use para aprofundar):")
        linhas += [f"- {r.get('pergunta')}: {r.get('resposta')}" for r in respostas]
    return "\n".join(linhas)


# ── resumo ao SDR ────────────────────────────────────────────────────────
ROTULO_CLASSIFICACAO = {"passa_direto": "Passa direto", "passa_alerta": "Passa com alerta",
                        "redireciona": "Redireciona (tráfego isolado)", "esteira": "Esteira",
                        "desqualifica": "Desqualificado", "em_andamento": "Triagem incompleta"}


def _v(fatos, k):
    v = (fatos or {}).get(k)
    if not conhecido(v):
        return "não informado"
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return ROTULO_FAIXA.get(v, str(v)) if isinstance(v, str) else (_mil(v) if k == "faturamento_valor" else str(v))


def consistencia(fatos: dict) -> str:
    """Sinais de consistência do faturamento (o que dá para checar sem julgar)."""
    sinais = []
    faixa, valor = (fatos or {}).get("faixa_faturamento"), (fatos or {}).get("faturamento_valor")
    if conhecido(faixa) and conhecido(valor) and faixa in FAIXAS:
        lo, hi = FAIXAS[faixa]
        sinais.append("valor dito bate com a faixa" if lo <= float(valor) <= hi
                      else f"valor dito ({_mil(float(valor))}) não bate com a faixa ({ROTULO_FAIXA[faixa]})")
    if (fatos or {}).get("investe_anuncio") == "sim" and conhecido((fatos or {}).get("valor_anuncio")):
        sinais.append(f"investe em anúncio ({fatos['valor_anuncio']})")
    if conhecido((fatos or {}).get("faixa_refino")):
        sinais.append("refino: " + ("mais perto do topo" if fatos["faixa_refino"] == "perto_do_maior"
                                    else "mais perto da base"))
    return "; ".join(sinais) or "sem sinais adicionais"


def resumo_sdr(pessoa: dict, fatos: dict, res: Resultado | None, link: str | None,
               agendamento: dict | None = None, resumo_ia: str | None = None) -> str:
    fatos = fatos or {}
    fat = _v(fatos, "faturamento_valor") if conhecido(fatos.get("faturamento_valor")) else _v(fatos, "faixa_faturamento")
    linhas = [
        f"Nome: {pessoa.get('nome') or 'não informado'}",
        f"Empresa: {pessoa.get('empresa') or 'não informada'}",
        f"Telefone: {pessoa.get('telefone') or 'não informado'}",
        f"Segmento: {_v(fatos, 'segmento')}",
        f"Cidade: {_v(fatos, 'cidade')}",
        f"Faturamento: {fat} (consistência: {consistencia(fatos)})",
        f"Investimento em anúncio: {_v(fatos, 'investe_anuncio')}; quanto: {_v(fatos, 'valor_anuncio')}",
        f"Marketing e conteúdo hoje: {_v(fatos, 'como_atrai_clientes')}",
        f"Dor/objetivo (palavras do lead): {_v(fatos, 'dor')}",
        f"Decisor: {_v(fatos, 'decisor')}"
        + (f" (contato: {fatos['contato_decisor']})" if conhecido(fatos.get("contato_decisor")) else ""),
        f"Quando quer começar: {_v(fatos, 'prazo_inicio')}",
        f"Experiência com agência: {_v(fatos, 'experiencia_agencia')}",
        f"Objeções: {_v(fatos, 'objecoes')}",
    ]
    if conhecido(fatos.get("assuntos_comerciais")):
        linhas.append(f"Perguntou sobre (o consultor trata): {_v(fatos, 'assuntos_comerciais')}")
    if res is not None:
        linhas.append(f"Classificação: {ROTULO_CLASSIFICACAO.get(res.classificacao, res.classificacao)} — "
                      f"{res.justificativa}")
        if res.alertas:
            linhas.append("Alertas: " + "; ".join(res.alertas))
    linhas.append(f"Conversa: {link or 'não disponível'}")
    linhas.append("Horário escolhido: " + ((agendamento or {}).get("rotulo") or "nenhum"))
    if resumo_ia and resumo_ia.strip():
        linhas.append("Resumo da IA: " + resumo_ia.strip())
    return "\n".join(linhas)
