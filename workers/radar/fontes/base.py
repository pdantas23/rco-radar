"""Interfaces dos adaptadores de fontes externas.

Regra de arquitetura: cada fonte fica atrás de um adaptador com a mesma
interface. Trocar Google Places por Outscraper, ou Evolution por outra, não pode
exigir mudança fora do adaptador. Padrão herdado do DiscoveryProvider do Harvey:
metadados + estimativa de custo ficam na base; a subclasse só implementa a busca.

Todo adaptador informa o custo do que fez (`custo_ultima_chamada`, em BRL),
para o worker gravar em custos_api.
"""

from dataclasses import dataclass, field
from datetime import date, datetime


class Adaptador:
    chave: str = ""                 # ex.: "google_places", "outscraper"
    rotulo: str = ""
    env: tuple[str, ...] = ()       # variáveis de ambiente exigidas
    custo_unitario_brl: float = 0.0 # por resultado/consulta, usado na estimativa
    limite_por_hora: int | None = None

    def configurado(self, env: dict) -> bool:
        return all(env.get(k) for k in self.env)

    def estimar(self, quantidade: int) -> float:
        return round(quantidade * self.custo_unitario_brl, 4)

    custo_ultima_chamada: float = 0.0


# ── F2 · Maps ─────────────────────────────────────────────────────────────
@dataclass
class ConsultaMaps:
    termo: str
    lat: float
    lng: float
    raio_km: float
    categoria: str | None = None
    limite: int = 60


@dataclass
class Avaliacao:
    autor: str
    nota: int
    texto: str
    data: datetime | None


@dataclass
class NegocioMaps:
    place_id: str
    nome: str
    categoria: str
    endereco: str
    cidade: str
    estado: str
    cep: str
    lat: float
    lng: float
    telefone: str | None
    site: str | None
    nota: float | None
    qtd_avaliacoes: int | None
    horario: dict | None
    link_maps: str
    fechado_permanentemente: bool
    whatsapp_link: str | None = None
    avaliacoes: list[Avaliacao] = field(default_factory=list)   # no máximo 5
    bruto: dict | None = None        # resposta completa, já sem dado de pessoa física


class FonteMaps(Adaptador):
    async def buscar(self, consulta: ConsultaMaps) -> list[NegocioMaps]:
        raise NotImplementedError


# ── F3 · CNPJ ─────────────────────────────────────────────────────────────
@dataclass
class DadosCnpj:
    cnpj: str
    razao_social: str
    nome_fantasia: str
    situacao: str
    abertura: date | None
    porte: str | None
    natureza: str | None
    capital_social: float | None
    cnae_principal: str | None
    cnae_descricao: str | None
    cnaes_secundarios: list[dict]
    regime: str | None
    socios: list[dict]               # só {nome, qualificacao}
    email_rf: str | None
    telefone_rf: str | None
    logradouro: str
    numero: str
    municipio: str
    uf: str
    cep: str
    eh_filial: bool
    cnpj_matriz: str
    qtd_filiais: int | None = None
    faturamento_faixa: str | None = None   # quando o fornecedor entrega (base externa)
    bruto: dict = field(default_factory=dict)   # {"casa": ..., "brasilapi": ...}, sem CPF


class FonteCnpj(Adaptador):
    async def buscar_por_nome(self, nome: str, municipio: str, uf: str) -> list[DadosCnpj]:
        raise NotImplementedError


# ── F5 · Instagram ────────────────────────────────────────────────────────
@dataclass
class PerfilInstagram:
    usuario: str
    seguidores: int | None
    seguindo: int | None
    posts_total: int | None
    ultimo_post_em: datetime | None
    posts_30d: int | None
    bio: str
    bio_tem_link: bool
    bio_tem_whatsapp: bool
    conta_comercial: bool | None
    bruto: dict | None = None


class FonteInstagram(Adaptador):
    async def perfil(self, usuario: str) -> PerfilInstagram | None:
        raise NotImplementedError

    async def procurar(self, nome: str, cidade: str) -> list[str]:
        """Candidatos a @ por nome + cidade (homônimos possíveis)."""
        raise NotImplementedError


# ── F6 · Anúncios ─────────────────────────────────────────────────────────
@dataclass
class ResultadoAnuncios:
    pagina_id: str | None
    pagina_nome: str | None
    qtd_ativos: int | None            # None = não verificado
    mais_antigo_em: date | None
    link_biblioteca: str | None
    amostras: list[dict]              # até 3 {texto, link_criativo}


class FonteAnuncios(Adaptador):
    async def verificar(self, pagina: str | None, nome: str, pais: str = "BR") -> ResultadoAnuncios:
        raise NotImplementedError


# ── F9/F10 · Canais de mensagem ───────────────────────────────────────────
@dataclass
class Envio:
    ok: bool
    id_externo: str | None = None
    motivo_falha: str | None = None   # numero_invalido | sem_whatsapp | conta_bloqueada | ...


class CanalMensagem(Adaptador):
    tipo: str = ""                    # whatsapp_oficial | whatsapp_nao_oficial | instagram

    async def enviar_texto(self, conta: str, destino: str, texto: str) -> Envio:
        raise NotImplementedError

    async def enviar_template(self, conta: str, destino: str, template: str, variaveis: dict) -> Envio:
        raise NotImplementedError

    async def numero_tem_whatsapp(self, conta: str, destino: str) -> bool | None:
        return None
