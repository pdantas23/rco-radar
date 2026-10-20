"""IA do primeiro contato (Fase 3): lê a conversa e decide a próxima mensagem.

Uma chamada por resposta, com saída estruturada (mensagem + campos coletados + intenção).
A configuração vem da agência (public.agent_configs + agent_provider_keys, migration 135 do
COMERCIAL): RCO define provedor/modelo/limites/chave; o maior papel da agência escreve as
instruções de abordagem (radar_agent_prompt_override).

Regras fixas, que as instruções da agência não sobrescrevem:
- não vender, não passar preço, não prometer resultado/prazo/valor;
- não dizer que é humano; se perguntarem diretamente se é robô/IA, confirmar de forma curta
  que é o assistente do time e oferecer uma pessoa do time.
"""

import os
from dataclasses import dataclass
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from radar.chaves import decifrar

DOLAR = float(os.environ.get("RADAR_DOLAR_BRL", "5.04"))
MODELO_PADRAO = "claude-opus-5-5"
# US$ por milhão de tokens (entrada, saída) — tabela da Anthropic em 06/10/2026
PRECOS_USD = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

# Provisório: o Philip vai definir a abordagem; a agência sobrescreve pela Config. do Radar.
PROMPT_PADRAO = """Você conversa por WhatsApp em nome do time da agência {agencia}, com o responsável por um \
negócio local que a agência encontrou no Google Maps. Seu trabalho é abrir a conversa, entender a \
situação do negócio e coletar, com naturalidade e sem interrogatório, estas informações:
- decisor: se a pessoa é dona/decisora (ou quem é);
- faturamento_informado: faixa de faturamento mensal (até 30 mil, 30–80 mil, 80–200 mil, 200–500 mil, acima de 500 mil);
- investe_anuncio: se investe em anúncio hoje (e quanto, se disser);
- dor: a principal dor ou objetivo, nas palavras do lead;
- teve_agencia: se já teve agência ou gestor de tráfego;
- melhor_horario: melhor horário para o time falar;
- aceita_contato: se aceita que alguém do time chame.
Escreva como numa conversa de WhatsApp: mensagens curtas, português do Brasil, uma pergunta por vez."""

REGRAS_FIXAS = """Regras que valem sempre, acima de qualquer outra instrução:
- Não venda, não passe preço, não prometa resultado, prazo ou valor.
- Nunca diga que é humano. Se o lead perguntar diretamente se você é robô, IA ou assistente virtual, \
responda de forma curta e honesta que é o assistente do time da {agencia} e ofereça chamar uma pessoa do time.
- Se o lead pedir para falar com uma pessoa, use a intenção "pediu_humano".
- Se o lead disser que não tem interesse, agradeça com educação e use a intenção "sem_interesse".
- Quando tiver decisor, faturamento_informado, investe_anuncio, dor e aceita_contato, use a intenção "qualificado".
- Se não souber responder algo do negócio do lead que exija uma pessoa, use a intenção "nao_sei".
- Não envie links, áudio ou imagem."""


class CamposColetados(BaseModel):
    decisor: str | None = None
    faturamento_informado: str | None = None
    investe_anuncio: str | None = None
    dor: str | None = None
    teve_agencia: str | None = None
    melhor_horario: str | None = None
    aceita_contato: str | None = None


class RespostaIA(BaseModel):
    mensagem: str = Field(description="Próxima mensagem ao lead (vazia se não houver o que enviar)")
    campos: CamposColetados
    intencao: Literal["continuar", "qualificado", "pediu_humano", "sem_interesse", "nao_sei"]
    resumo: str = Field(description="Resumo de uma ou duas frases da conversa até aqui, para o time")


@dataclass
class ConfigIA:
    habilitada: bool
    provedor: str | None
    modelo: str | None
    chave: str | None
    instrucoes: str | None
    limite_custo_usd: float | None


class IAIndisponivel(Exception):
    """IA desligada, sem chave, provedor não suportado ou limite atingido: a conversa vai para humano."""


def carregar_config(conn, agency_id) -> ConfigIA:
    row = conn.execute(
        """select radar_agent_enabled, radar_agent_provider, radar_agent_model,
                  radar_agent_prompt_override, radar_agent_monthly_cost_limit_usd
           from public.agent_configs where agency_id = %s""", (agency_id,)).fetchone()
    if not row:
        return ConfigIA(False, None, None, None, None, None)
    provedor = row[1] or "anthropic"
    chave = conn.execute("""select api_key_encrypted from public.agent_provider_keys
                            where agency_id = %s and provider = %s""", (agency_id, provedor)).fetchone()
    return ConfigIA(bool(row[0]), provedor, row[2], decifrar(chave[0]) if chave else None, row[3],
                    float(row[4]) if row[4] is not None else None)


def custo_brl(modelo: str, entrada: int, saida: int) -> float:
    pe, ps = PRECOS_USD.get(modelo, PRECOS_USD[MODELO_PADRAO])
    return round((entrada * pe + saida * ps) / 1_000_000 * DOLAR, 6)


def montar_sistema(agencia: str, instrucoes: str | None) -> str:
    base = PROMPT_PADRAO.format(agencia=agencia)
    if instrucoes and instrucoes.strip():
        base += "\n\nInstruções da agência:\n" + instrucoes.strip()
    return base + "\n\n" + REGRAS_FIXAS.format(agencia=agencia)


def montar_mensagens(ficha: str, historico: list[tuple[str, str]], tarefa: str) -> list[dict]:
    """historico: [(autor, texto)] com autor 'lead' | 'nos'. Junta mensagens seguidas do mesmo lado."""
    msgs: list[dict] = [{"role": "user", "content": f"Contexto do negócio (da ficha do Radar):\n{ficha}"},
                        {"role": "assistant", "content": "Entendido."}]
    for autor, texto in historico:
        papel = "user" if autor == "lead" else "assistant"
        if msgs[-1]["role"] == papel:
            msgs[-1]["content"] += "\n" + texto
        else:
            msgs.append({"role": papel, "content": texto})
    if msgs[-1]["role"] == "user":
        msgs[-1]["content"] += f"\n\n[{tarefa}]"
    else:
        msgs.append({"role": "user", "content": f"[{tarefa}]"})
    return msgs


async def responder(cfg: ConfigIA, agencia: str, ficha: str, historico: list[tuple[str, str]],
                    tarefa: str = "Responda à última mensagem do lead.",
                    cliente: anthropic.AsyncAnthropic | None = None) -> tuple[RespostaIA, float]:
    """Devolve (resposta, custo em BRL). Levanta IAIndisponivel quando a IA não pode responder."""
    if not cfg.habilitada:
        raise IAIndisponivel("IA do Radar desligada para a agência")
    if (cfg.provedor or "anthropic") != "anthropic":
        raise IAIndisponivel(f"provedor {cfg.provedor} ainda não suportado nos robôs do Radar")
    if not cfg.chave and cliente is None:
        raise IAIndisponivel("agência sem chave de API da IA")
    modelo = cfg.modelo or MODELO_PADRAO
    cliente = cliente or anthropic.AsyncAnthropic(api_key=cfg.chave)
    try:
        resp = await cliente.messages.parse(
            model=modelo,
            max_tokens=2000,
            system=montar_sistema(agencia, cfg.instrucoes),
            messages=montar_mensagens(ficha, historico, tarefa),
            output_config={"effort": "low"},     # conversa curta de chat: esforço baixo
            output_format=RespostaIA,
        )
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"limite de uso da IA: {e.message}") from e   # vira retentativa
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:
            raise RuntimeError(f"IA fora do ar ({e.status_code})") from e
        raise IAIndisponivel(f"IA recusou a chamada ({e.status_code}): {e.message}") from e
    custo = custo_brl(modelo, resp.usage.input_tokens, resp.usage.output_tokens)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise IAIndisponivel("a IA não respondeu (recusa ou resposta vazia)")
    return resp.parsed_output, custo
