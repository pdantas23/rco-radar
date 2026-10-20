from radar.privacidade import limpar


def test_remove_cpf_de_socio_em_qualquer_nivel():
    casa = {"cnpj": "1", "quadro_societario": [{"nome": "JOSE", "qualificacao_socio": "Titular",
                                                 "documento": "***123456**", "cpf_representante_legal": "x",
                                                 "faixa_etaria_descricao": "41 a 50"}]}
    brasil = {"qsa": [{"nome_socio": "JOSE", "cnpj_cpf_do_socio": "***123456**", "identificador_socio": 2}]}
    assert limpar(casa)["quadro_societario"] == [{"nome": "JOSE", "qualificacao_socio": "Titular"}]
    assert limpar(brasil)["qsa"] == [{"nome_socio": "JOSE"}]


def test_avaliacao_sem_quem_escreveu():
    # "reviewerBadge" não está na lista proibida: só sai porque avaliação usa lista do que PODE ficar
    item = {"title": "Barbearia", "reviews": [{"name": "Maria Silva", "reviewerId": "9", "reviewerUrl": "u",
                                               "reviewerBadge": "Maria S.", "text": "boa", "stars": 5,
                                               "publishedAtDate": "2026-10-01"}]}
    assert limpar(item)["reviews"] == [{"text": "boa", "stars": 5, "publishedAtDate": "2026-10-01"}]
    assert limpar(item)["title"] == "Barbearia"


def test_instagram_sem_terceiros():
    item = {"username": "ze", "fullName": "Barbearia do Zé", "latestPosts": [
        {"caption": "promo", "latestComments": [{"ownerUsername": "cliente1"}], "taggedUsers": ["a"]}],
        "relatedProfiles": [{"username": "outro"}]}
    limpo = limpar(item)
    assert limpo == {"username": "ze", "fullName": "Barbearia do Zé", "latestPosts": [{"caption": "promo"}]}
