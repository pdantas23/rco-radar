"""IA do Radar pelo OpenRouter (decisão do Philip, 17/11/2026: IA dos clientes só via OpenRouter)."""

import json

import httpx
import pytest
import respx

from radar import ia

CFG = ia.ConfigIA(True, "openrouter", "openai/gpt-5.6-luna", "sk-or-teste", None, None)
RESPOSTA = {
    "mensagem": "Oi! Tudo bem? Você é o responsável pela loja?",
    "campos": {k: None for k in ia.CamposColetados.model_fields},
    "intencao": "continuar",
    "resumo": "Primeiro contato.",
}


def _ok(conteudo, custo=0.0012):
    return httpx.Response(200, json={"choices": [{"message": {"content": conteudo}}],
                                     "usage": {"prompt_tokens": 900, "completion_tokens": 80, "cost": custo}})


@respx.mock
async def test_responde_pelo_openrouter_com_esquema_e_custo_real():
    rota = respx.post(ia.OPENROUTER_URL).mock(return_value=_ok(json.dumps(RESPOSTA)))
    async with httpx.AsyncClient() as http:
        r, custo = await ia.responder(CFG, "RCO HUB", "Loja X, Teresina", [("lead", "oi")], http=http)
    assert r.intencao == "continuar" and r.mensagem.startswith("Oi!")
    assert custo == round(0.0012 * ia.DOLAR, 6)
    corpo = json.loads(rota.calls[0].request.content)
    assert corpo["model"] == "openai/gpt-5.6-luna"
    assert corpo["messages"][0]["role"] == "system" and "RCO HUB" in corpo["messages"][0]["content"]
    assert corpo["response_format"]["json_schema"]["strict"] is True
    assert corpo["usage"] == {"include": True}
    assert rota.calls[0].request.headers["authorization"] == "Bearer sk-or-teste"


@respx.mock
async def test_sem_modelo_usa_o_padrao():
    rota = respx.post(ia.OPENROUTER_URL).mock(return_value=_ok(json.dumps(RESPOSTA)))
    cfg = ia.ConfigIA(True, "openrouter", None, "sk", None, None)
    async with httpx.AsyncClient() as http:
        await ia.responder(cfg, "A", "f", [("lead", "oi")], http=http)
    assert json.loads(rota.calls[0].request.content)["model"] == ia.OPENROUTER_MODELO_PADRAO


@respx.mock
async def test_resposta_fora_do_formato_vai_para_humano():
    respx.post(ia.OPENROUTER_URL).mock(return_value=_ok("não é json"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ia.IAIndisponivel, match="formato"):
            await ia.responder(CFG, "A", "f", [("lead", "oi")], http=http)


@respx.mock
async def test_chave_recusada_vai_para_humano_e_fora_do_ar_tenta_de_novo():
    respx.post(ia.OPENROUTER_URL).mock(side_effect=[
        httpx.Response(401, json={"error": {"message": "No auth credentials found"}}),
        httpx.Response(503, json={"error": {"message": "down"}}),
        httpx.Response(429, json={"error": {"message": "slow down"}}),
    ])
    async with httpx.AsyncClient() as http:
        with pytest.raises(ia.IAIndisponivel, match="401"):
            await ia.responder(CFG, "A", "f", [("lead", "oi")], http=http)
        with pytest.raises(RuntimeError, match="fora do ar"):
            await ia.responder(CFG, "A", "f", [("lead", "oi")], http=http)
        with pytest.raises(RuntimeError, match="limite"):
            await ia.responder(CFG, "A", "f", [("lead", "oi")], http=http)


async def test_provedor_nao_suportado_e_sem_chave():
    with pytest.raises(ia.IAIndisponivel, match="não suportado"):
        await ia.responder(ia.ConfigIA(True, "gemini", None, "k", None, None), "A", "f", [("lead", "oi")])
    with pytest.raises(ia.IAIndisponivel, match="sem chave"):
        await ia.responder(ia.ConfigIA(True, "openrouter", None, None, None, None), "A", "f", [("lead", "oi")])


def test_esquema_estrito_sem_referencias():
    texto = json.dumps(ia.ESQUEMA_RESPOSTA)
    assert "$ref" not in texto and "$defs" not in texto
    assert set(ia.ESQUEMA_RESPOSTA["properties"]["campos"]["required"]) == set(ia.CamposColetados.model_fields)


# ── triagem v2 pelo OpenRouter (merge da feat/triagem-ia, 18/11/2026) ────
def _todos_os_nos(n, caminho="$"):
    if isinstance(n, dict):
        yield caminho, n
        for k, v in n.items():
            yield from _todos_os_nos(v, f"{caminho}.{k}")
    elif isinstance(n, list):
        for i, v in enumerate(n):
            yield from _todos_os_nos(v, f"{caminho}[{i}]")


def test_esquema_v2_no_formato_estrito_do_openrouter():
    sch = ia.ESQUEMA_RESPOSTA
    assert set(sch["required"]) == {"mensagem", "campos", "fatos", "intencao", "escolha_horario", "resumo",
                                     "reacao"}
    assert set(sch["properties"]["fatos"]["required"]) == set(ia.Fatos.model_fields)
    for caminho, n in _todos_os_nos(sch):
        assert not {"$ref", "$defs", "anyOf", "oneOf", "allOf", "default", "title"} & set(n), caminho
        if n.get("type") == "object":
            assert n["additionalProperties"] is False and set(n["required"]) == set(n["properties"]), caminho
    # opcional = união de tipos com null (sem anyOf)
    assert sch["properties"]["fatos"]["properties"]["faturamento_valor"]["type"] == ["number", "null"]
    assert all(p["type"] == ["string", "null"] for p in sch["properties"]["campos"]["properties"].values())
    assert sch["properties"]["escolha_horario"]["enum"] == ["opcao_1", "opcao_2", "nenhuma", "indefinido"]
    # reação: texto livre no schema (emoji fora da lista não pode derrubar a resposta; o código filtra)
    assert sch["properties"]["reacao"]["type"] == "string" and "enum" not in sch["properties"]["reacao"]


def test_schema_estrito_recusa_uniao_que_o_openrouter_nao_aceitaria():
    from pydantic import BaseModel

    class Ruim(BaseModel):
        x: int | str

    with pytest.raises(ValueError, match="união não suportada"):
        ia.schema_estrito(Ruim)


RESPOSTA_V2 = {
    "mensagem": "E vocês investem em anúncio hoje?",
    "campos": {k: None for k in ia.CamposColetados.model_fields},
    "fatos": {**{k: "" for k in ("segmento", "tempo_mercado", "como_atrai_clientes", "valor_anuncio", "dor",
                                  "contato_decisor", "cidade", "experiencia_agencia")},
              **{k: "indefinido" for k in ("investe_anuncio", "necessidade_clara", "decisor", "prazo_inicio",
                                            "faixa_faturamento", "faixa_refino", "ve_marketing_como_despesa",
                                            "orcamento_ou_intencao", "pede_algo_fora")},
              "segmento": "Barbearia", "decisor": "dono_socio", "faturamento_valor": None,
              "objecoes": [], "assuntos_comerciais": ["preco"]},
    "intencao": "continuar",
    "escolha_horario": "indefinido",
    "resumo": "Dono da barbearia.",
}


@respx.mock
async def test_triagem_v2_pelo_openrouter_devolve_fatos_e_usa_o_prompt_v2():
    assert set(RESPOSTA_V2["fatos"]) == set(ia.Fatos.model_fields)          # resposta completa, como o strict exige
    rota = respx.post(ia.OPENROUTER_URL).mock(return_value=_ok(json.dumps(RESPOSTA_V2), custo=0.0004))
    async with httpx.AsyncClient() as http:
        r, custo = await ia.responder(CFG, "RCO HUB", "Negócio: Barbearia 0", [("lead", "Sou o dono")],
                                      "Próximo assunto: marketing.", http=http, origem="radar", v2=True)
    assert r.fatos.decisor == "dono_socio" and r.fatos.assuntos_comerciais == ["preco"]
    assert r.fatos.faturamento_valor is None and r.escolha_horario == "indefinido"
    assert custo == round(0.0004 * ia.DOLAR, 6)
    corpo = json.loads(rota.calls[0].request.content)
    assert corpo["response_format"]["json_schema"]["schema"] == ia.ESQUEMA_RESPOSTA
    assert "seguindo um roteiro" in corpo["messages"][0]["content"]
    assert "Próximo assunto: marketing." in corpo["messages"][-1]["content"]
    assert rota.calls[0].request.headers["x-title"] == "RCO Radar"


@respx.mock
async def test_rede_fora_tenta_de_novo_e_402_vai_para_humano():
    respx.post(ia.OPENROUTER_URL).mock(side_effect=[httpx.ConnectError("sem rede"),
                                                    httpx.Response(402, json={"error": {"message": "sem créditos"}}),
                                                    httpx.Response(200, text="<html>")])
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="fora do ar"):
            await ia.responder(CFG, "A", "f", [("lead", "oi")], http=http, v2=True)
        with pytest.raises(ia.IAIndisponivel, match="402"):
            await ia.responder(CFG, "A", "f", [("lead", "oi")], http=http, v2=True)
        with pytest.raises(ia.IAIndisponivel, match="formato"):
            await ia.responder(CFG, "A", "f", [("lead", "oi")], http=http, v2=True)


def test_sem_eco_corta_a_abertura_que_repete_o_lead():
    r = ia.RespostaIA.model_validate({**RESPOSTA, "mensagem": "Entendi, hoje vocês atraem clientes só no orgânico. Esse movimento vem mais de indicação, Instagram ou Google?"})
    assert ia.sem_eco(r).mensagem == "Esse movimento vem mais de indicação, Instagram ou Google?"
    r = ia.RespostaIA.model_validate({**RESPOSTA, "mensagem": "Entendi, vocês ainda não trabalharam com agência. Quem costuma decidir sobre marketing aí?"})
    assert ia.sem_eco(r).mensagem == "Quem costuma decidir sobre marketing aí?"


def test_sem_eco_nao_mexe_quando_nao_ha_pergunta_depois_ou_nao_comeca_com_eco():
    for msg in ("Entendi. Obrigado pelas respostas!", "Show! O que mais trava o crescimento hoje?", "Boa tarde! Tudo bem?"):
        r = ia.RespostaIA.model_validate({**RESPOSTA, "mensagem": msg})
        assert ia.sem_eco(r).mensagem == msg
