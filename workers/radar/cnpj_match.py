"""Casamento ficha do Maps × CNPJ (F3).

Confiança de 0 a 100 com o motivo legível ("nome fantasia e endereço iguais").
Abaixo de 50 o CNPJ não é assumido. Mais de um candidato acima de 50 com
diferença pequena => revisão humana com a lista.

Trava: só passa do limiar quem tem nome parecido E localização compatível (mesma rua
ou mesmo CEP), ou telefone igual. Nome sozinho (outro "Maverick" do outro lado da cidade)
ou endereço sozinho (outra barbearia do mesmo CEP) não bastam. O nome é comparado sem as
palavras do ramo ("barbearia", "barber shop"), senão toda "X Barbearia" parece com "Y Barbearia".
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

LIMIAR_ACEITE = 50
MARGEM_EMPATE = 10        # dois candidatos a menos de 10 pontos => humano decide
TETO_SEM_TRAVA = LIMIAR_ACEITE - 5   # sem nome + localização (ou telefone) não chega ao limiar

# Palavras que não distinguem um negócio de outro.
RUIDO_NOME = {
    "ltda", "me", "epp", "eireli", "mei", "sa", "s/a", "cia", "comercio", "servicos",
    "de", "da", "do", "das", "dos", "e", "a", "o", "the",
}
# Palavras do ramo e de unidade: aparecem em metade dos nomes e não identificam ninguém.
GENERICO_NOME = {
    "barbearia", "barbearias", "barber", "barbers", "barbershop", "shop", "salao", "saloes", "beleza",
    "cabeleireiro", "cabeleireira", "cabeleireiros", "estetica", "studio", "estudio", "espaco", "restaurante",
    "lanchonete", "pizzaria", "padaria", "hamburgueria", "cafeteria", "cafe", "bar", "clinica", "consultorio",
    "odontologia", "odontologica", "loja", "store", "academia", "oficina", "pet", "petshop", "materiais",
    "construcao", "farmacia", "drogaria", "otica", "auto", "center", "centro", "matriz", "filial", "unidade",
    "celular", "celulares", "cell", "cel", "acessorios", "assistencia", "tecnica", "eletronicos", "eletros",
    "informatica", "imports", "importados", "phone", "phones", "iphone", "telefonia",
    "em", "no", "na", "ii", "iii",
}


def palavras_do_ramo(ramo: str) -> set[str]:
    """'Loja de celular · Loja de celulares' -> {'loja', 'celular', 'celulares'}: o nicho da própria pesquisa
    também não identifica ninguém (a lista fixa nunca cobre todo ramo). Inclui singular e plural simples."""
    out = set()
    for p in normalizar_nome(ramo).split():
        out |= {p, p[:-1] if p.endswith("s") else p + "s", p[:-2] if p.endswith("es") else p}
    return out
TIPOS_LOGRADOURO = r"(?:r|rua|av|avenida|al|alameda|tv|trav|travessa|pc|pca|praca|rod|rodovia|est|estrada|q|qd|quadra)"
ABREV_ENDERECO = {
    "r": "rua", "av": "avenida", "trav": "travessa", "tv": "travessa", "al": "alameda",
    "pc": "praca", "pca": "praca", "rod": "rodovia", "est": "estrada", "q": "quadra",
}


def _base(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", t)


def normalizar_nome(nome: str) -> str:
    return " ".join(p for p in _base(nome).split() if p not in RUIDO_NOME)


def nome_maps(nome: str) -> str:
    """'Barba Negra | Barbearia em Teresina' -> 'Barba Negra': o Maps cola descrição depois de | ou -."""
    return re.split(r"\s+[|\-–]\s+", nome or "", maxsplit=1)[0]


def palavras_do_endereco(endereco: str, nome: str) -> set[str]:
    """Palavras do endereço do Maps (sem o nome que ele cola na frente): 'Rio Poty' do shopping, a rua.
    No nome do negócio elas dizem ONDE ele fica, não QUEM ele é."""
    bruto = _so_ascii(endereco)
    # só a partir do tipo de logradouro: antes dele o Maps às vezes repete o nome ("Loja Itemtudo Cell Av. ...")
    m = re.search(r"(?:^|[\s,\-|])" + TIPOS_LOGRADOURO + r"\.?\s", bruto)
    if not m:
        return set()
    return {p for p in _base(bruto[m.start():]).split() if not p.isdigit()}


def nome_distintivo(nome: str, cidade: str = "", ramo: str = "", vazio_se_generico: bool = False,
                    tirar_tambem: set[str] = frozenset()) -> str:
    """Nome sem ruído, sem palavras do ramo (lista fixa + nicho da pesquisa) e sem o nome da cidade.

    Sobrando nada: o nome normalizado, ou "" com vazio_se_generico (o negócio do Maps chamado só "Celular"
    não identifica ninguém e não pode casar com nenhum CNPJ).
    """
    tirar = GENERICO_NOME | set(_base(cidade).split()) | palavras_do_ramo(ramo) | set(tirar_tambem)
    norm = normalizar_nome(nome)
    sobra = " ".join(p for p in norm.split() if p not in tirar)
    return sobra if sobra or vazio_se_generico else norm


def _so_ascii(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()


def normalizar_endereco(end: str) -> tuple[str, str]:
    """(logradouro normalizado, número) — o resto do endereço é ruído para casar.

    O endereço do Maps vem como 'Nome do Negócio - Av. X, 123 - Bairro, Cidade - UF, CEP': procura
    primeiro o trecho 'tipo de logradouro + nome, número'; sem isso, o trecho que começa com o tipo.
    """
    bruto = _so_ascii(end)
    m = re.search(r"(?:^|[\s,\-|])(" + TIPOS_LOGRADOURO + r"\.?\s[^,|]+?),\s*(\d+)", bruto)
    if m:
        partes = [ABREV_ENDERECO.get(p, p) for p in _base(m.group(1)).split()]
        return " ".join(partes), m.group(2)
    trechos = re.split(r"\s+-\s+", bruto)
    for t in trechos:
        if re.match(TIPOS_LOGRADOURO + r"\.?\s", t.strip()):
            end = t
            break
    partes = _base(end).split()
    partes = [ABREV_ENDERECO.get(p, p) for p in partes]
    numero = next((p for p in partes if p.isdigit()), "")
    logradouro = []
    for p in partes:
        if p.isdigit():
            break
        logradouro.append(p)
    return " ".join(logradouro), numero


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb)
    return max(jaccard, SequenceMatcher(None, a, b).ratio())


def _similar_nome(a: str, b: str) -> tuple[float, bool]:
    """(semelhança, veio só de 'um nome contido no outro'). Contido = 'bunker' em 'bunker av rio poti'.

    Contido sozinho é evidência fraca ('dirceu' em 'dirceu vet'): quem chama decide se vale.
    """
    if not a or not b:
        return 0.0, False
    ta, tb = set(a.split()), set(b.split())
    if ta != tb and (ta <= tb or tb <= ta):
        return 0.8, True                        # 'dirceu' × 'dirceu vet' também dá 0,75 por letras: é contido
    return _similar(a, b), False


def _nome_inteiro(a: str, b: str) -> tuple[float, bool]:
    """Nome inteiro (com as palavras do ramo): só igual ou contido contam. Sem a semelhança por letras,
    que faria 'X Barbearia' parecer 'Y Barbearia'. Salva 'Shop do Celular' = 'SHOP DO CELULAR'."""
    if not a or not b:
        return 0.0, False
    if a == b:
        return 1.0, False
    ta, tb = set(a.split()), set(b.split())
    return (0.8, True) if (ta <= tb or tb <= ta) else (0.0, False)


@dataclass
class Negocio:
    nome: str
    endereco: str
    cidade: str
    cep: str = ""
    telefone: str = ""          # E.164
    ramo: str = ""              # nicho + categoria da pesquisa ("Loja de celular Loja de celulares")


@dataclass
class Candidato:
    cnpj: str
    razao_social: str
    nome_fantasia: str
    logradouro: str
    numero: str
    municipio: str
    cep: str = ""
    telefone: str = ""          # E.164
    situacao: str = ""


@dataclass
class Resultado:
    candidato: Candidato
    confianca: int
    motivo: str
    # nome idêntico, mas sem endereço/CEP/telefone que confirme: não assume, pede um humano
    so_nome_igual: bool = False


def pontuar(negocio: Negocio, cand: Candidato) -> Resultado:
    """Soma evidências; cada uma vira um trecho do motivo exibido na ficha."""
    if _base(negocio.cidade).strip() != _base(cand.municipio).strip():
        return Resultado(cand, 0, "cidade diferente")

    pontos, motivos = 20, ["mesma cidade"]
    nome_m = nome_maps(negocio.nome)
    alvo = nome_distintivo(nome_m, negocio.cidade, negocio.ramo, vazio_se_generico=True,
                           tirar_tambem=palavras_do_endereco(negocio.endereco, negocio.nome))
    inteiro = normalizar_nome(nome_m)
    notas = []                                  # (semelhança, só contido, é fantasia)
    for texto, fantasia in ((cand.nome_fantasia, True), (cand.razao_social, False)):
        notas.append((*_similar_nome(alvo, nome_distintivo(texto, cand.municipio, negocio.ramo)), fantasia))
        notas.append((*_nome_inteiro(inteiro, normalizar_nome(texto)), fantasia))
    # a melhor nota; empate prefere a que não veio só de "contido"
    sim_nome, so_contido, pela_fantasia = max(notas, key=lambda n: (n[0], not n[1]))
    tem_nome = sim_nome >= 0.75
    if sim_nome >= 0.95:
        pontos += 40
        motivos.append("nome fantasia igual" if pela_fantasia else "razão social igual")
    elif sim_nome >= 0.75:
        pontos += 25
        motivos.append("nome contido" if so_contido else "nome parecido")

    log_m, num_m = normalizar_endereco(negocio.endereco)
    log_c = normalizar_endereco(cand.logradouro)[0]
    tem_local = local_forte = False
    if _similar(log_m, log_c) >= 0.85:
        tem_local = True
        if num_m and num_m == re.sub(r"\D", "", cand.numero or ""):
            local_forte = True
            pontos += 30
            motivos.append("endereço igual")
        else:
            pontos += 15
            motivos.append("mesma rua")

    cep_m, cep_c = re.sub(r"\D", "", negocio.cep), re.sub(r"\D", "", cand.cep)
    if cep_m and cep_m == cep_c:
        tem_local = local_forte = True
        pontos += 5
        motivos.append("CEP igual")
    tem_tel = bool(negocio.telefone) and negocio.telefone == cand.telefone
    if tem_tel:
        pontos += 20
        motivos.append("telefone igual")

    # nome só "contido" ('dirceu' em 'dirceu vet') precisa de número da rua ou CEP iguais, mesma rua não basta
    if tem_nome and so_contido and not local_forte:
        tem_nome = False
    so_nome_igual = False
    if not tem_tel and not (tem_nome and tem_local):
        pontos = min(pontos, TETO_SEM_TRAVA)
        motivos.append("endereço não confere" if tem_nome or sim_nome >= 0.75 else "nome não confere")
        # revisão humana só quando o nome identifica alguém (não para um negócio chamado só "Celular")
        so_nome_igual = sim_nome >= 0.95 and bool(alvo)

    situacao = (cand.situacao or "").strip().upper()
    if situacao and situacao != "ATIVA":
        pontos -= 15
        motivos.append(f"situação {situacao.lower()}")

    return Resultado(cand, max(0, min(100, pontos)), ", ".join(motivos), so_nome_igual)


@dataclass
class Decisao:
    escolhido: Resultado | None       # None => cnpj_status 'nao_encontrado' ou 'revisao'
    revisao_humana: bool
    candidatos: list[Resultado]       # todos, ordenados, para guardar


def decidir(negocio: Negocio, candidatos: list[Candidato]) -> Decisao:
    res = sorted((pontuar(negocio, c) for c in candidatos), key=lambda r: r.confianca, reverse=True)
    aceitos = [r for r in res if r.confianca >= LIMIAR_ACEITE]
    if not aceitos:
        # mesmo nome sem endereço que confirme (outra filial, base sem endereço, homônimo): humano decide
        return Decisao(None, any(r.so_nome_igual for r in res), res)
    if len(aceitos) > 1 and aceitos[0].confianca - aceitos[1].confianca < MARGEM_EMPATE:
        return Decisao(None, True, res)
    return Decisao(aceitos[0], False, res)


def texto_ficha(r: Resultado) -> str:
    """'CNPJ casado com confiança 85%: nome fantasia igual, endereço igual'."""
    return f"CNPJ casado com confiança {r.confianca}%: {r.motivo}"
