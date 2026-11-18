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
