"""Limpa dados de pessoa física antes de gravar respostas completas das fontes.

O Radar guarda dados do NEGÓCIO. Nunca grava CPF (nem mascarado) de sócio,
nem quem escreveu uma avaliação ou comentou num post.
"""

# chaves removidas em qualquer nível
CHAVES_PROIBIDAS = {
    # Receita / Casa dos Dados / BrasilAPI
    "documento", "cpf", "cnpj_cpf_do_socio", "identificador_socio", "cpf_representante_legal",
    "nome_representante_legal", "qualificacao_representante_legal", "faixa_etaria", "faixa_etaria_codigo",
    "faixa_etaria_descricao", "codigo_faixa_etaria",
    # Google Maps: quem avaliou
    "name", "reviewerId", "reviewerUrl", "reviewerPhotoUrl", "reviewerNumberOfReviews", "isLocalGuide",
    "reviewId", "reviewUrl", "reviewImageUrls", "reviewContext", "reviewDetailedRating",
    # Instagram: terceiros
    "latestComments", "taggedUsers", "relatedProfiles", "ownerUsername", "ownerFullName", "ownerId",
    "coauthorProducers", "mentions",
}
# em itens de avaliação/comentário, só estas chaves sobrevivem
CHAVES_AVALIACAO = {"text", "stars", "publishedAtDate", "likesCount", "responseFromOwnerText",
                    "responseFromOwnerDate", "textTranslated"}


def limpar(obj, _pai: str = ""):
    if isinstance(obj, dict):
        if _pai == "reviews":
            return {k: limpar(v, k) for k, v in obj.items() if k in CHAVES_AVALIACAO}
        return {k: limpar(v, k) for k, v in obj.items() if k not in CHAVES_PROIBIDAS}
    if isinstance(obj, list):
        return [limpar(v, _pai) for v in obj]
    return obj
