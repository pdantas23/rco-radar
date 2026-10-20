"""F4 · Faturamento estimado e F5 · presença digital.

Regras simples, com pesos editáveis pelo admin (radar.config). O resultado
sempre vem com o texto que a ficha mostra, e o faturamento é sempre faixa.
"""

from dataclasses import dataclass, field
from datetime import date

FAIXAS = ["ate_30k", "30k_80k", "80k_200k", "200k_500k", "acima_500k"]

# Ponto de partida por porte (índice em FAIXAS). MEI tem teto legal de faturamento.
BASE_PORTE = {"MEI": 0, "ME": 0, "EPP": 2, "DEMAIS": 4}
PESOS_FATURAMENTO = {
    "avaliacoes_muitas": 150,       # nº de avaliações que soma +1 faixa
    "ritmo_alto": 4.0,              # avaliações por mês que soma +1 faixa
    "filiais_soma": 1,              # 2+ unidades somam +1 faixa
    "seguidores_muitos": 10000,     # seguidores que somam +1 faixa
    "capital_alto": 100000,         # capital social que soma +1 faixa
}
CNAE_TICKET_ALTO = ("86", "69", "71", "47")     # saúde, contabilidade, engenharia, varejo


@dataclass
class DadosFaturamento:
    porte: str | None = None
    capital_social: float | None = None
    qtd_avaliacoes: int | None = None
    abertura: date | None = None
    unidades: int = 1
    cnae: str | None = None
    tem_site: bool = False
    seguidores: int | None = None
    faixa_base_externa: str | None = None


@dataclass
class Estimativa:
    faixa: str
    origem: str | None            # base_externa | calculado | None (sem estimativa)
    explicacao: str


def estimar_faturamento(d: DadosFaturamento, pesos: dict | None = None, hoje: date | None = None) -> Estimativa:
    if d.faixa_base_externa in FAIXAS:
        return Estimativa(d.faixa_base_externa, "base_externa", "Informado por base externa")
    p = {**PESOS_FATURAMENTO, **(pesos or {})}
    hoje = hoje or date.today()
    sinais, nivel = [], None

    if d.porte in BASE_PORTE:
        nivel = BASE_PORTE[d.porte]
        sinais.append(f"porte {d.porte}")
    av = d.qtd_avaliacoes or 0
    if av:
        nivel = nivel or 0
        sinais.append(f"{av} avaliações")
        if av >= p["avaliacoes_muitas"]:
            nivel += 1
        if d.abertura:
            meses = max(1, (hoje - d.abertura).days / 30.4)
            if av / meses >= p["ritmo_alto"]:
                nivel += 1
                sinais.append("ritmo alto de avaliações")
    if d.unidades >= 2:
        nivel = (nivel or 0) + p["filiais_soma"]
        sinais.append(f"{d.unidades} unidades")
    if (d.seguidores or 0) >= p["seguidores_muitos"]:
        nivel = (nivel or 0) + 1
        sinais.append(f"{d.seguidores} seguidores")
    if (d.capital_social or 0) >= p["capital_alto"]:
        nivel = (nivel or 0) + 1
        sinais.append("capital social alto")
    if d.cnae and d.cnae.startswith(CNAE_TICKET_ALTO) and nivel is not None:
        nivel += 1
        sinais.append("setor de ticket alto")

    if nivel is None:
        return Estimativa("sem_estimativa", None, "Sem dados suficientes para estimar")
    if d.porte == "MEI":
        nivel = 0                    # teto legal do MEI
    faixa = FAIXAS[max(0, min(len(FAIXAS) - 1, nivel))]
    return Estimativa(faixa, "calculado", "Estimado pelo sistema a partir de: " + ", ".join(sinais))


# ── F5 · presença digital ────────────────────────────────────────────────
REGRA_PRESENCA = {"seguidores_min": 1000, "posts_30d_min": 4, "media": 3, "forte": 4}


@dataclass
class DadosPresenca:
    instagram_verificado: bool = False
    tem_instagram: bool = False
    seguidores: int | None = None
    posts_30d: int | None = None
    site_verificado: bool = False
    site_responde: bool = False
    tem_pixel_ou_tag: bool = False
    pontos_detalhe: list = field(default_factory=list)


def presenca_digital(d: DadosPresenca, regra: dict | None = None) -> str:
    """nenhuma | fraca | media | forte | nao_verificado. Exemplo do levantamento => media."""
    r = {**REGRA_PRESENCA, **(regra or {})}
    if not d.instagram_verificado and not d.site_verificado:
        return "nao_verificado"
    pontos = 0
    pontos += 1 if d.tem_instagram else 0
    pontos += 1 if (d.seguidores or 0) >= r["seguidores_min"] else 0
    pontos += 1 if (d.posts_30d or 0) >= r["posts_30d_min"] else 0
    pontos += 1 if d.site_responde else 0
    pontos += 1 if d.tem_pixel_ou_tag else 0
    if pontos == 0:
        return "nenhuma"
    if pontos >= r["forte"]:
        return "forte"
    if pontos >= r["media"]:
        return "media"
    return "fraca"
