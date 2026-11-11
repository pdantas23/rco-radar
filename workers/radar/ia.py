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

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import anthropic
import httpx
from pydantic import BaseModel, Field

from radar import custo_zero
from radar.chaves import decifrar

DOLAR = float(os.environ.get("RADAR_DOLAR_BRL", "5.04"))
MODELO_PADRAO = "claude-opus-5-5"
# US$ por milhão de tokens (entrada, saída) — tabela da Anthropic em 06/10/2026
PRECOS_USD = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    # OpenRouter (tabela do analisador-comercial, config/precos.json)
    "openai/gpt-5.6-luna": (0.20, 1.20),
}
MODELO_PADRAO_OPENROUTER = "openai/gpt-5.6-luna"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PROVEDORES = ("anthropic", "openrouter")

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

# Provisório (exemplo até o Miguel definir): lead que veio de landing page (triagem IA, origem 'lp').
PROMPT_LP = """Você conversa por WhatsApp em nome do time da agência {agencia}, com uma pessoa que preencheu o \
formulário de uma landing page da agência e pediu contato. As respostas do formulário estão no contexto: \
NÃO pergunte de novo o que já foi respondido; use essas respostas para seguir a conversa. Seu trabalho é \
entender o momento do negócio e completar, com naturalidade e sem interrogatório, o que ainda falta saber:
- decisor: se a pessoa é dona/decisora (ou quem é);
- faturamento_informado: faixa de faturamento mensal (até 30 mil, 30–80 mil, 80–200 mil, 200–500 mil, acima de 500 mil);
- investe_anuncio: se investe em anúncio hoje (e quanto, se disser);
- dor: a principal dor ou objetivo, nas palavras da pessoa;
- teve_agencia: se já teve agência ou gestor de tráfego;
- melhor_horario: melhor horário para o time falar;
- aceita_contato: se aceita que alguém do time chame.
Escreva como numa conversa de WhatsApp: mensagens curtas, português do Brasil, uma pergunta por vez."""

# A IA só registra fatos; a nota e a decisão saem do código (radar/triagem.py).
REGRA_FATOS = """Em "fatos", registre só o que o lead disse na conversa ou respondeu no formulário, e use \
"indefinido" para o que ainda não se sabe. Não julgue se o lead é bom: a decisão não é sua."""

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


SimNao = Literal["sim", "nao", "indefinido"]


class Fatos(BaseModel):
    """Fatos normalizados que alimentam a nota (radar/triagem.py). 'indefinido' = ainda não se sabe."""
    decisor: SimNao = "indefinido"
    faturamento_faixa: Literal["ate_30k", "30k_80k", "80k_200k", "200k_500k", "acima_500k", "indefinido"] = "indefinido"
    investe_anuncio: SimNao = "indefinido"
    dor_clara: SimNao = Field("indefinido", description="o lead descreveu uma dor ou objetivo concreto")
    urgencia: Literal["alta", "media", "baixa", "indefinido"] = "indefinido"
    teve_agencia: SimNao = "indefinido"
    aceita_contato: SimNao = "indefinido"


class RespostaIA(BaseModel):
    mensagem: str = Field(description="Próxima mensagem ao lead (vazia se não houver o que enviar)")
    campos: CamposColetados
    fatos: Fatos = Field(default_factory=Fatos)
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


class LimiteCusto(IAIndisponivel):
    """Gasto do mês com a IA do Radar chegou ao limite_custo_usd do agente: o modelo não é chamado."""


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
    # modelo fora da tabela: preço do modelo mais caro (superestima, nunca subestima o teto)
    pe, ps = PRECOS_USD.get(modelo, PRECOS_USD[MODELO_PADRAO])
    return round((entrada * pe + saida * ps) / 1_000_000 * DOLAR, 6)


def gasto_mes_usd(conn, agency_id, agora: datetime) -> float:
    """Gasto da IA do Radar (fonte 'ia_radar') no mês corrente, em US$ (custos_api guarda BRL)."""
    brl = conn.execute(
        """select coalesce(sum(valor), 0) from radar.custos_api
           where agency_id = %s and fonte = 'ia_radar' and em >= date_trunc('month', %s::timestamptz)""",
        (agency_id, agora)).fetchone()[0]
    return float(brl) / DOLAR


def checar_orcamento(conn, cfg: ConfigIA, agency_id, agora: datetime) -> None:
    """Aplica limite_custo_usd (mensal). Sem limite configurado, não bloqueia."""
    if cfg.limite_custo_usd is None:
        return
    gasto = gasto_mes_usd(conn, agency_id, agora)
    if gasto >= cfg.limite_custo_usd:
        raise LimiteCusto(f"limite de custo da IA atingido: US$ {gasto:.2f} de US$ {cfg.limite_custo_usd:.2f} no mês")


def montar_sistema(agencia: str, instrucoes: str | None, origem: str = "radar") -> str:
    base = (PROMPT_LP if origem == "lp" else PROMPT_PADRAO).format(agencia=agencia)
    if instrucoes and instrucoes.strip():
        base += "\n\nInstruções da agência:\n" + instrucoes.strip()
    return base + "\n\n" + REGRA_FATOS + "\n\n" + REGRAS_FIXAS.format(agencia=agencia)


def montar_mensagens(ficha: str, historico: list[tuple[str, str]], tarefa: str, origem: str = "radar") -> list[dict]:
    """historico: [(autor, texto)] com autor 'lead' | 'nos'. Junta mensagens seguidas do mesmo lado."""
    titulo = "Contexto do lead (da landing page)" if origem == "lp" else "Contexto do negócio (da ficha do Radar)"
    msgs: list[dict] = [{"role": "user", "content": f"{titulo}:\n{ficha}"},
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
                    cliente=None, origem: str = "radar") -> tuple[RespostaIA, float]:
    """Devolve (resposta, custo em BRL). Levanta IAIndisponivel quando a IA não pode responder.

    `cliente` (testes): AsyncAnthropic-like para 'anthropic'; httpx.AsyncClient para 'openrouter'."""
    custo_zero.exigir_liberado("IA")
    if not cfg.habilitada:
        raise IAIndisponivel("IA do Radar desligada para a agência")
    provedor = cfg.provedor or "anthropic"
    if provedor not in PROVEDORES:
        raise IAIndisponivel(f"provedor {cfg.provedor} ainda não suportado nos robôs do Radar")
    if not cfg.chave and cliente is None:
        raise IAIndisponivel("agência sem chave de API da IA")
    sistema = montar_sistema(agencia, cfg.instrucoes, origem)
    mensagens = montar_mensagens(ficha, historico, tarefa, origem)
    if provedor == "openrouter":
        return await _openrouter(cfg, sistema, mensagens, cliente)
    modelo = cfg.modelo or MODELO_PADRAO
    cliente = cliente or anthropic.AsyncAnthropic(api_key=cfg.chave)
    try:
        resp = await cliente.messages.parse(
            model=modelo,
            max_tokens=2000,
            system=sistema,
            messages=mensagens,
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


# ── OpenRouter (Chat Completions compatível com OpenAI; base: analisador-comercial providers/openrouter.py) ──
def schema_estrito(modelo: type[BaseModel]) -> dict:
    """JSON Schema do pydantic no formato `strict` (refs resolvidas, todos os campos obrigatórios, sem extras)."""
    bruto = modelo.model_json_schema()
    defs = bruto.pop("$defs", {})

    def conv(n):
        if isinstance(n, list):
            return [conv(x) for x in n]
        if not isinstance(n, dict):
            return n
        if "$ref" in n:
            return conv(defs[n["$ref"].split("/")[-1]])
        n = {k: conv(v) for k, v in n.items() if k not in ("default", "title")}
        if n.get("type") == "object" and "properties" in n:
            n["required"] = list(n["properties"])
            n["additionalProperties"] = False
        return n

    return conv(bruto)


async def _openrouter(cfg: ConfigIA, sistema: str, mensagens: list[dict],
                      cliente: httpx.AsyncClient | None) -> tuple[RespostaIA, float]:
    modelo = cfg.modelo or MODELO_PADRAO_OPENROUTER
    payload = {
        "model": modelo,
        "messages": [{"role": "system", "content": sistema}, *mensagens],
        "max_tokens": 2000,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "resposta", "strict": True, "schema": schema_estrito(RespostaIA)}},
        "usage": {"include": True},
    }
    http = cliente or httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0))
    try:
        r = await http.post(OPENROUTER_URL, json=payload, headers={"Authorization": f"Bearer {cfg.chave or ''}"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"OpenRouter inacessível: {type(e).__name__}") from e   # vira retentativa
    finally:
        if cliente is None:
            await http.aclose()
    if r.status_code == 429 or r.status_code >= 500:
        raise RuntimeError(f"OpenRouter fora do ar ou no limite ({r.status_code})")
    if r.status_code >= 400:                       # 402 = sem créditos; 401/403/400 = chave/modelo
        raise IAIndisponivel(f"OpenRouter recusou a chamada ({r.status_code}): {r.text[:200]}")
    dados = r.json()
    uso = dados.get("usage") or {}
    if uso.get("cost") is not None:                # custo cobrado de verdade (US$), quando a OpenRouter informa
        custo = round(float(uso["cost"]) * DOLAR, 6)
    else:
        custo = custo_brl(modelo, uso.get("prompt_tokens") or 0, uso.get("completion_tokens") or 0)
    texto = (((dados.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    try:
        return RespostaIA.model_validate(json.loads(texto)), custo
    except (ValueError, TypeError) as e:
        raise IAIndisponivel(f"a IA não respondeu no formato esperado: {texto[:200]}") from e
