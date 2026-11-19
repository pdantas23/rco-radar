"""Toque humano nas conversas da IA no WhatsApp (pedido do Philip, 19/11/2026).

1. "digitando...": antes de cada resposta (nunca na 1ª mensagem do disparo) o chat do lead mostra "digitando..."
   por um tempo proporcional ao tamanho do texto (1,5 s + 35 ms por caractere, no máximo 8 s). Rota do COMERCIAL:
   POST /api/internal/radar/digitando (Evolution Go POST /message/presence, state "composing"). O indicador é
   renovado a cada 5 s enquanto a espera dura. Falha de presença só vira aviso no log: a resposta sai do mesmo jeito,
   e sem presença não há espera.
2. Reação: a IA pode devolver `reacao` (um emoji). Aqui só passa emoji da lista fechada e só em resposta positiva
   (intenção "continuar" ou "qualificado"); o resto é descartado em silêncio. O ritmo (no máximo 1 reação a cada 4
   mensagens do robô) fica no COMERCIAL (POST /api/internal/radar/reagir), que é quem enxerga as reações já feitas
   (public.message_reactions). A reação vai para a ÚLTIMA mensagem do lead; sem id dela, não reage.

As esperas usam asyncio.sleep: o laço de contato e a fila de jobs seguem andando enquanto um chat "digita".
"""

import asyncio
import logging

log = logging.getLogger("radar.toque_humano")

EMOJIS_REACAO = ("👍", "😊", "🙌", "👏", "❤️")
INTENCOES_COM_REACAO = ("continuar", "qualificado")

DIGITANDO_BASE_S = 1.5
DIGITANDO_POR_CARACTERE_S = 0.035
DIGITANDO_MAX_S = 8.0
RENOVAR_A_CADA_S = 5.0            # o "digitando..." do WhatsApp some sozinho se não for renovado

# Trocável nos testes (sem espera de verdade)
dormir = asyncio.sleep


def duracao_digitando(texto: str) -> float:
    """Segundos de "digitando..." para uma mensagem: 1,5 s + 35 ms por caractere, no máximo 8 s."""
    return round(min(DIGITANDO_BASE_S + DIGITANDO_POR_CARACTERE_S * len((texto or "").strip()), DIGITANDO_MAX_S), 3)


def filtrar_reacao(reacao: str | None, intencao: str) -> str | None:
    """Emoji que pode sair, ou None. Fora da lista fechada, mais de um emoji ou intenção que não é positiva: None."""
    e = (reacao or "").strip()
    if e == "❤":                                   # coração sem o seletor de variação
        e = "❤️"
    if e not in EMOJIS_REACAO or intencao not in INTENCOES_COM_REACAO:
        return None
    return e


async def digitar(comercial, *, agency_id, conversation_id, texto: str) -> float:
    """Mostra "digitando..." e espera o tempo do texto. Devolve os segundos esperados (0 se a presença falhou)."""
    total = duracao_digitando(texto)
    try:
        r = await comercial.digitando(agency_id=agency_id, conversation_id=conversation_id)
    except Exception as e:                           # noqa: BLE001 — presença nunca segura a resposta
        log.warning("digitando falhou (conversa %s): %s", conversation_id, e)
        return 0.0
    if not r.get("ok"):
        log.warning("digitando recusado (conversa %s): %s", conversation_id, r.get("motivo"))
        return 0.0
    restante = total
    while restante > 0:
        passo = min(RENOVAR_A_CADA_S, restante)
        await dormir(passo)
        restante = round(restante - passo, 3)
        if restante > 0:
            try:
                await comercial.digitando(agency_id=agency_id, conversation_id=conversation_id)
            except Exception as e:                   # noqa: BLE001
                log.info("renovar digitando falhou (conversa %s): %s", conversation_id, e)
    return total


async def reagir(comercial, *, agency_id, conversation_id, mensagens_do_lead: list[dict], reacao: str | None,
                 intencao: str) -> str | None:
    """Reage à última mensagem do lead, se a IA pediu e o filtro deixou. Devolve o emoji enviado (ou None)."""
    emoji = filtrar_reacao(reacao, intencao)
    if emoji is None or not mensagens_do_lead:
        return None
    alvo = mensagens_do_lead[-1].get("id")
    if not alvo:
        return None
    try:
        r = await comercial.reagir(agency_id=agency_id, conversation_id=conversation_id, message_id=alvo, emoji=emoji)
    except Exception as e:                           # noqa: BLE001 — reação é enfeite, nunca derruba a conversa
        log.warning("reação falhou (conversa %s): %s", conversation_id, e)
        return None
    if not r.get("ok") or r.get("pulado"):
        if not r.get("ok"):
            log.info("reação recusada (conversa %s): %s", conversation_id, r.get("motivo"))
        return None
    return emoji
