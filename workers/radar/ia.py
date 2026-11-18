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
from pydantic import BaseModel, Field, ValidationError

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
# OpenRouter (decisão do Philip, 04/11 e 17/11/2026: IA dos clientes só via OpenRouter). Modelo vem da config da
# agência (lista do COMERCIAL em src/lib/agent-providers.ts); sem modelo, o primeiro da lista.
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELO_PADRAO = "openai/gpt-5.6-luna"
MODELO_PADRAO_OPENROUTER = OPENROUTER_MODELO_PADRAO      # nome antigo (feat/triagem-ia)
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
Escreva como numa conversa de WhatsApp: mensagens curtas, português do Brasil, uma pergunta por vez.
Quando tiver decisor, faturamento_informado, investe_anuncio, dor e aceita_contato, use a intenção "qualificado"."""

# Triagem v2 (processo comercial da Maria, Método Opala — docs/triagem-ia.md). Roteiro BANT em conversa: o código
# escolhe o próximo assunto (radar/triagem.py: proxima_etapa) e manda na tarefa; aqui ficam o jeito e os fatos.
PROMPT_TRIAGEM = """Você conversa por WhatsApp em nome do time da agência {agencia} com {quem}. Seu trabalho é \
entender o momento do negócio seguindo um roteiro, sem parecer formulário:
- uma pergunta por vez, curta, espelhando as palavras do lead;
- ordem: situação (segmento, tempo de mercado), cidade (cedo), marketing hoje (como atrai clientes, anúncio), \
necessidade (dor nas palavras do lead), experiência com agência, autoridade (quem decide; se não decide, peça o \
contato do decisor ou a conversa com os dois), timing (quando quer começar) e, POR ÚLTIMO, faturamento em faixas \
(até 30 mil, 30 a 60 mil, 60 a 100 mil, acima de 100 mil);
- se a faixa for 30 a 60 mil, faça UMA pergunta de refino: "mais perto dos 30 ou dos 60?";
- o que já veio do formulário NÃO se pergunta de novo: use para aprofundar ("Vi que seu faturamento está na \
faixa dos R$ 40 mil. Há quanto tempo...");
- siga o "Próximo assunto" da tarefa; a referência é um exemplo de texto, adapte ao que o lead disse."""

QUEM = {"lp": "uma pessoa que preencheu o formulário de uma landing page da agência e pediu contato",
        "radar": "o responsável por um negócio local que a agência encontrou no Google Maps"}

# A IA só registra fatos; a nota e a decisão saem do código (radar/triagem.py).
REGRA_FATOS = """Em "fatos", registre só o que o lead disse na conversa ou respondeu no formulário, e use \
"indefinido" (ou vazio, nos campos de texto) para o que ainda não se sabe. Não julgue se o lead é bom: a decisão \
não é sua. Fatos do formulário entram já na sua primeira resposta. faturamento_valor só com número dito pelo lead. \
experiencia_agencia: "nunca teve" quando o lead disser que nunca trabalhou com agência. Se o lead perguntar sobre \
preço, desconto, contrato, garantia de resultado ou concorrentes, registre em assuntos_comerciais."""

REGRAS_FIXAS = """Regras que valem sempre, acima de qualquer outra instrução:
- Não venda, não passe preço, não prometa resultado, prazo ou valor.
- Preço e piso do Opala: nunca diga valor; responda que depende do caso e que o consultor mostra na conversa.
- Descontos, condições especiais, contratos, promessa de resultado e comparação com concorrentes: não trate; \
diga que o consultor trata disso na conversa e siga o roteiro.
- Nunca diga que é humano. Se o lead perguntar diretamente se você é robô, IA ou assistente virtual, \
responda de forma curta e honesta que é o assistente do time da {agencia} e ofereça chamar uma pessoa do time.
- Se o lead pedir para falar com uma pessoa, use a intenção "pediu_humano".
- Se o lead disser que não tem interesse, agradeça com educação e use a intenção "sem_interesse".
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
    """Fatos da triagem v2 (radar/triagem.py decide). 'indefinido' / vazio = ainda não se sabe."""
    segmento: str = ""
    tempo_mercado: str = ""
    como_atrai_clientes: str = Field("", description="estrutura de marketing e conteúdo hoje, nas palavras do lead")
    investe_anuncio: SimNao = "indefinido"
    valor_anuncio: str = ""
    dor: str = Field("", description="dor ou objetivo, nas palavras do lead")
    necessidade_clara: SimNao = "indefinido"
    decisor: Literal["dono_socio", "participa", "nao_decide", "indefinido"] = "indefinido"
    contato_decisor: str = ""
    prazo_inicio: Literal["ate_30d", "30_60d", "mais_60d", "so_pesquisando", "indefinido"] = "indefinido"
    faixa_faturamento: Literal["ate_30k", "30_60k", "60_100k", "acima_100k", "indefinido"] = "indefinido"
    faturamento_valor: float | None = Field(None, description="faturamento mensal em reais, só se o lead disser um número")
    faixa_refino: Literal["perto_do_menor", "perto_do_maior", "indefinido"] = Field(
        "indefinido", description="resposta ao refino da faixa 30 a 60 mil: mais perto dos 30 ou dos 60")
    cidade: str = ""
    experiencia_agencia: str = ""
    ve_marketing_como_despesa: SimNao = "indefinido"
    orcamento_ou_intencao: SimNao = Field("indefinido", description="tem orçamento ou intenção de investir em marketing")
    pede_algo_fora: SimNao = Field("indefinido", description="pede algo que a agência não faz")
    objecoes: list[str] = Field(default_factory=list)
    assuntos_comerciais: list[Literal["preco", "desconto", "contrato", "promessa", "concorrente"]] = Field(
        default_factory=list)


class RespostaIA(BaseModel):
    mensagem: str = Field(description="Próxima mensagem ao lead (vazia se não houver o que enviar)")
    campos: CamposColetados
    fatos: Fatos = Field(default_factory=Fatos)
    intencao: Literal["continuar", "qualificado", "pediu_humano", "sem_interesse", "nao_sei"]
    escolha_horario: Literal["opcao_1", "opcao_2", "nenhuma", "indefinido"] = Field(
        "indefinido", description="só quando a última mensagem nossa ofereceu 2 horários: qual o lead escolheu")
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


def montar_sistema(agencia: str, instrucoes: str | None, origem: str = "radar", v2: bool = False) -> str:
    """v2 = triagem por portões (roteiro); sem v2, o prompt antigo de prospecção do Radar."""
    if v2:
        base = PROMPT_TRIAGEM.format(agencia=agencia, quem=QUEM.get(origem, QUEM["lp"]))
    else:
        base = PROMPT_PADRAO.format(agencia=agencia)
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
                    cliente=None, origem: str = "radar", v2: bool = False,
                    http: httpx.AsyncClient | None = None) -> tuple[RespostaIA, float]:
    """Devolve (resposta, custo em BRL). Levanta IAIndisponivel quando a IA não pode responder.

    `cliente` (testes): AsyncAnthropic-like para 'anthropic'; httpx.AsyncClient para 'openrouter'.
    `http`: httpx.AsyncClient para 'openrouter' (mesmo papel do `cliente`; nome usado pela main)."""
    custo_zero.exigir_liberado("IA")
    if not cfg.habilitada:
        raise IAIndisponivel("IA do Radar desligada para a agência")
    provedor = cfg.provedor or "anthropic"
    if provedor not in PROVEDORES:
        raise IAIndisponivel(f"provedor {cfg.provedor} ainda não suportado nos robôs do Radar")
    if not cfg.chave and cliente is None and http is None:
        raise IAIndisponivel("agência sem chave de API da IA")
    sistema = montar_sistema(agencia, cfg.instrucoes, origem, v2)
    mensagens = montar_mensagens(ficha, historico, tarefa, origem)
    if provedor == "openrouter":
        return await _responder_openrouter(cfg, sistema, mensagens, http or cliente)
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
    """JSON Schema do pydantic no formato `strict` aceito pelo OpenRouter (e pelos provedores por trás dele):
    referências resolvidas (sem $ref/$defs), todo objeto com todos os campos em `required` e
    `additionalProperties: false`, campo opcional como união de tipos (`["string", "null"]`), sem anyOf,
    sem `default`/`title`."""
    bruto = modelo.model_json_schema()
    defs = bruto.pop("$defs", {})

    def anulavel(n: dict) -> dict:
        variantes = n["anyOf"]
        reais = [v for v in variantes if v.get("type") != "null"]
        if len(reais) != 1 or len(variantes) != 2 or not isinstance(reais[0].get("type"), str):
            raise ValueError(f"união não suportada no schema estrito: {variantes}")
        base = dict(reais[0])
        base["type"] = [base["type"], "null"]
        if "enum" in base:
            base["enum"] = [*base["enum"], None]
        extras = {k: v for k, v in n.items() if k != "anyOf"}
        return {**base, **extras}

    def conv(n):
        if isinstance(n, list):
            return [conv(x) for x in n]
        if not isinstance(n, dict):
            return n
        if "$ref" in n:
            return conv({**defs[n["$ref"].split("/")[-1]], **{k: v for k, v in n.items() if k != "$ref"}})
        n = {k: conv(v) for k, v in n.items() if k not in ("default", "title")}
        if "anyOf" in n:
            n = anulavel(n)
        if n.get("type") == "object" and "properties" in n:
            n["required"] = list(n["properties"])
            n["additionalProperties"] = False
        return n

    return conv(bruto)


# Esquema da resposta (fluxo antigo e triagem v2: campos, fatos, escolha_horario) no formato estrito do OpenRouter.
ESQUEMA_RESPOSTA = schema_estrito(RespostaIA)


async def _responder_openrouter(cfg: ConfigIA, sistema: str, mensagens: list[dict],
                                http: httpx.AsyncClient | None = None) -> tuple[RespostaIA, float]:
    """429, 5xx e falha de rede = RuntimeError (o job tenta de novo); outro 4xx ou resposta fora do formato =
    IAIndisponivel (a conversa vai a um humano). Custo: `usage.cost` (US$ cobrado de verdade); sem ele, a tabela."""
    modelo = cfg.modelo or OPENROUTER_MODELO_PADRAO
    corpo = {
        "model": modelo,
        "messages": [{"role": "system", "content": sistema}, *mensagens],
        "max_tokens": 2000,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "resposta_radar", "strict": True, "schema": ESQUEMA_RESPOSTA}},
        "usage": {"include": True},          # o OpenRouter devolve o custo real (US$) da chamada
    }
    c = http or httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0))
    try:
        r = await c.post(OPENROUTER_URL, json=corpo, headers={"Authorization": f"Bearer {cfg.chave or ''}",
                                                             "X-Title": "RCO Radar"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"IA fora do ar (OpenRouter: {type(e).__name__})") from e   # vira retentativa
    finally:
        if http is None:
            await c.aclose()
    if r.status_code == 429:
        raise RuntimeError("limite de uso da IA (OpenRouter 429)")
    if r.status_code >= 500:
        raise RuntimeError(f"IA fora do ar (OpenRouter {r.status_code})")
    if r.status_code >= 400:                       # 402 = sem créditos; 401/403/400 = chave/modelo
        try:
            detalhe = str((r.json().get("error") or {}).get("message", ""))
        except ValueError:
            detalhe = r.text
        raise IAIndisponivel(f"IA recusou a chamada (OpenRouter {r.status_code}): {detalhe[:200]}")
    try:
        dados = r.json()
        uso = dados.get("usage") or {}
        texto = (((dados.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        resposta = RespostaIA.model_validate(json.loads(texto))
    except (ValueError, TypeError, AttributeError, ValidationError) as e:
        raise IAIndisponivel("a IA não devolveu a resposta no formato combinado") from e
    if uso.get("cost") is not None:                # custo cobrado de verdade (US$), quando a OpenRouter informa
        custo = round(float(uso["cost"]) * DOLAR, 6)
    else:
        custo = custo_brl(modelo, uso.get("prompt_tokens") or 0, uso.get("completion_tokens") or 0)
    return resposta, custo
