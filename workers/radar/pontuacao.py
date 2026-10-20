"""Pontuação e classificação do lead (F7).

Função pura: recebe o lead já enriquecido e uma regra (pesos + faixas, editáveis
pelo admin e versionados por nicho) e devolve a conta aberta que a ficha mostra.
Nunca descarta: nota baixa só ordena por último.
"""

from dataclasses import dataclass, field

# Pesos iniciais do levantamento (exemplo da Barbearia do Zé soma 95).
PESOS_PADRAO = {
    "sem_anuncio_ativo": 25,
    "presenca_media_ou_forte": 15,
    "tem_whatsapp": 15,
    "faturamento_30k_ou_mais": 15,
    "google_bem_avaliado": 15,
    "cnpj_ativo": 10,
}
FAIXAS_PADRAO = {"quente": 70, "morno": 40}
PARAMS_PADRAO = {"nota_minima": 4.0, "min_avaliacoes": 50}

FAIXAS_FATURAMENTO_30K_MAIS = {"30k_80k", "80k_200k", "200k_500k", "acima_500k"}
CNPJ_BAIXADO = {"BAIXADA", "INAPTA", "NULA"}

ROTULOS = {
    "sem_anuncio_ativo": "Sem anúncio ativo",
    "presenca_media_ou_forte": "Presença digital Média ou Forte",
    "tem_whatsapp": "Tem WhatsApp",
    "faturamento_30k_ou_mais": "Faturamento estimado 30 mil ou mais",
    "google_bem_avaliado": "Nota Google e avaliações acima do mínimo",
    "cnpj_ativo": "CNPJ ativo",
    "zera_fechado_ou_baixado": "Fechado permanentemente ou CNPJ baixado (zera)",
    "zera_sem_canal": "Sem nenhum canal de contato (zera)",
}


@dataclass
class Regra:
    versao: int
    pesos: dict = field(default_factory=lambda: dict(PESOS_PADRAO))
    faixas: dict = field(default_factory=lambda: dict(FAIXAS_PADRAO))
    params: dict = field(default_factory=lambda: dict(PARAMS_PADRAO))
    nicho: str | None = None


@dataclass
class LeadParaPontuar:
    anuncio_ativo: str                    # sim | nao | nao_verificado
    presenca_digital: str | None
    tem_whatsapp_provavel: bool
    whatsapp: str | None
    instagram_user: str | None
    telefone: str | None
    faturamento_estimado_faixa: str | None
    nota: float | None
    qtd_avaliacoes: int | None
    cnpj_situacao: str | None             # None = sem CNPJ casado
    fechado_permanentemente: bool = False


@dataclass
class Criterio:
    nome: str
    rotulo: str
    bateu: bool
    pontos: int


@dataclass
class Pontuacao:
    total: int
    classificacao: str
    versao_regra: int
    criterios: list[Criterio]


def escolher_regra(regras: list[Regra], nicho: str | None) -> Regra:
    """Regra mais recente do nicho; senão a geral mais recente."""
    do_nicho = [r for r in regras if nicho and r.nicho and r.nicho.lower() == nicho.lower()]
    gerais = [r for r in regras if r.nicho is None]
    candidatas = do_nicho or gerais
    if not candidatas:
        raise ValueError("nenhuma regra de pontuação cadastrada")
    return max(candidatas, key=lambda r: r.versao)


def classificar(total: int, faixas: dict) -> str:
    if total >= faixas["quente"]:
        return "quente"
    if total >= faixas["morno"]:
        return "morno"
    return "frio"


def pontuar(lead: LeadParaPontuar, regra: Regra) -> Pontuacao:
    p = regra.params
    tem_whatsapp = bool(lead.whatsapp) or lead.tem_whatsapp_provavel
    checagens = {
        # "não verificado" não ganha ponto de "sem anúncio": falta de dado não é oportunidade
        "sem_anuncio_ativo": lead.anuncio_ativo == "nao",
        "presenca_media_ou_forte": lead.presenca_digital in ("media", "forte"),
        "tem_whatsapp": tem_whatsapp,
        "faturamento_30k_ou_mais": lead.faturamento_estimado_faixa in FAIXAS_FATURAMENTO_30K_MAIS,
        "google_bem_avaliado": (lead.nota or 0) >= p["nota_minima"]
                               and (lead.qtd_avaliacoes or 0) >= p["min_avaliacoes"],
        "cnpj_ativo": (lead.cnpj_situacao or "").upper() == "ATIVA",
    }
    criterios = [
        Criterio(nome, ROTULOS.get(nome, nome), bateu, regra.pesos.get(nome, 0) if bateu else 0)
        for nome, bateu in checagens.items()
    ]
    total = sum(c.pontos for c in criterios)

    fechado = lead.fechado_permanentemente or (lead.cnpj_situacao or "").upper() in CNPJ_BAIXADO
    sem_canal = not (tem_whatsapp or lead.telefone or lead.instagram_user)
    criterios.append(Criterio("zera_fechado_ou_baixado", ROTULOS["zera_fechado_ou_baixado"], fechado, 0))
    criterios.append(Criterio("zera_sem_canal", ROTULOS["zera_sem_canal"], sem_canal, 0))
    if fechado or sem_canal:
        total = 0

    total = max(0, min(100, total))
    return Pontuacao(total, classificar(total, regra.faixas), regra.versao, criterios)
