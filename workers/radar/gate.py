"""Trava determinística antes de cada envio (F9/F10).

Adaptado do pre-send gate do Harvey (MIT): instrução de prompt é piso, não
garantia. Todo envio passa por estas checagens em código no momento de sair;
qualquer falha bloqueia e grava o motivo em disparos_leads.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

VARIAVEL_RE = re.compile(r"\{\s*\w+\s*\}")
URL_RE = re.compile(r"https?://\S+|www\.\S+|\b\w+\.(com|com\.br|net|org)(/\S*)?\b", re.I)

# Frases que a IA nunca pode dizer (o prompt pede; o código garante).
PROIBIDAS = (
    "sou humano", "sou uma pessoa", "não sou um robô", "nao sou um robo", "não sou robô",
    "garanto", "garantido", "resultado garantido", "100%", "sem risco",
)
OPT_OUT_PALAVRAS = ("sair", "parar", "pare", "não quero", "nao quero", "remover", "descadastrar",
                    "me tira", "não me mande", "nao me mande", "stop")
DIAS_RECONTATO = 30


@dataclass
class Contexto:
    lead_status: str
    motivo_descarte: str | None
    bloqueado: bool                         # telefone ou @ na lista de bloqueio
    ultimo_contato_mesmo_canal: datetime | None
    confirmado_recontato: bool              # confirmação extra do humano (<30 dias)
    disparo_confirmado: bool
    registrado_em_disparos_leads: bool      # regra: registro ANTES da tentativa
    agora_local_lead: datetime              # horário local do lead
    janela_inicio: time
    janela_fim: time
    canal: str                              # whatsapp_oficial | whatsapp_nao_oficial | instagram
    template_aprovado: bool                 # só importa no whatsapp_oficial
    primeira_mensagem: bool
    tem_midia: bool = False


@dataclass
class Resultado:
    ok: bool
    motivos: list[str] = field(default_factory=list)

    def __bool__(self):
        return self.ok


def checar(texto: str, ctx: Contexto) -> Resultado:
    m: list[str] = []
    texto = (texto or "").strip()

    if not ctx.disparo_confirmado:
        m.append("disparo sem confirmação humana")
    if not ctx.registrado_em_disparos_leads:
        m.append("envio sem registro prévio em disparos_leads")
    # Recusa do lead não bloqueia sozinha (decisão do Philip, 20/10/2026): a empresa decide se e
    # quando volta a abordar. Só bloqueia o que alguém da agência pôs na lista de bloqueio à mão.
    if ctx.bloqueado:
        m.append("número bloqueado pela agência")
    if ctx.lead_status == "descartado":
        m.append("lead descartado (reabra antes de contatar)")
    if ctx.primeira_mensagem and ctx.ultimo_contato_mesmo_canal and not ctx.confirmado_recontato:
        if ctx.agora_local_lead - ctx.ultimo_contato_mesmo_canal < timedelta(days=DIAS_RECONTATO):
            m.append(f"já contatado neste canal há menos de {DIAS_RECONTATO} dias")
    hora = ctx.agora_local_lead.time()
    if not (ctx.janela_inicio <= hora < ctx.janela_fim):
        m.append(f"fora da janela {ctx.janela_inicio:%H:%M}-{ctx.janela_fim:%H:%M} do lead")
    if ctx.canal == "whatsapp_oficial" and ctx.primeira_mensagem and not ctx.template_aprovado:
        m.append("WhatsApp oficial só envia template aprovado pela Meta")

    if not texto:
        m.append("mensagem vazia")
    sobra = VARIAVEL_RE.findall(texto)
    if sobra:
        m.append(f"variável não preenchida: {sorted(set(sobra))[:3]}")
    baixo = texto.lower()
    hits = [p for p in PROIBIDAS if p in baixo]
    if hits:
        m.append(f"frase proibida: {hits[:3]}")
    if ctx.primeira_mensagem:
        if URL_RE.search(texto):
            m.append("link na primeira mensagem")
        if ctx.tem_midia:
            m.append("mídia na primeira mensagem")

    return Resultado(not m, m)


def pediu_para_parar(texto: str) -> bool:
    """Detecção conservadora de opt-out na resposta do lead (a IA também classifica)."""
    t = re.sub(r"\s+", " ", (texto or "").lower()).strip(" .!")
    if t in OPT_OUT_PALAVRAS:
        return True
    return any(re.search(rf"\b{re.escape(p)}\b", t) for p in OPT_OUT_PALAVRAS if " " in p)
