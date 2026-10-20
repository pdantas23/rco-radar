"""Conversores dos adaptadores, com itens no formato documentado de cada provedor.

Os formatos ainda não foram conferidos com chamada real (sem chaves): validar na 1ª rodada paga.
"""

from datetime import date, datetime, timezone

from radar.fontes import anuncios_apify, cnpj, instagram_apify, maps_apify


def test_maps_converte_e_limita_5_avaliacoes_sem_nome():
    item = {"placeId": "ChIJ1", "title": "Barbearia do Zé", "categoryName": "Barbearia",
            "address": "R. A, 100 - Centro, Teresina - PI, 64000-000", "city": "Teresina", "state": "State of Piauí",
            "postalCode": "64000-000", "location": {"lat": -5.09, "lng": -42.8}, "phoneUnformatted": "+5586999998888",
            "website": "https://ze.com.br", "totalScore": 4.6, "reviewsCount": 180, "url": "https://maps/x",
            "openingHours": [{"day": "segunda-feira", "hours": "08:00–18:00"}], "permanentlyClosed": False,
            "reviews": [{"name": "Fulano", "text": f"t{i}", "stars": 5, "publishedAtDate": f"2026-10-0{i+1}T10:00:00Z"} for i in range(8)]}
    n = maps_apify.converter(item)
    assert n.estado == "PI" and n.horario == {"segunda-feira": "08:00–18:00"}
    assert len(n.avaliacoes) == 5 and n.avaliacoes[0].texto == "t7" and n.avaliacoes[0].autor == ""
    assert maps_apify.converter({"title": "sem place_id", "address": "x"}) is None


def test_uf():
    assert maps_apify.uf_de("Piauí") == "PI" and maps_apify.uf_de("SP") == "SP" and maps_apify.uf_de("??") == ""


def test_entrada_do_ator_maps():
    from radar.fontes.base import ConsultaMaps
    e = maps_apify.MapsApify(token="x").entrada(ConsultaMaps("barbearia", -5.09, -42.8, 5.0, None, 80))
    assert e["customGeolocation"]["coordinates"] == [-42.8, -5.09] and e["maxReviews"] == 5
    assert e["scrapeReviewsPersonalData"] is False


def test_cnpj_casa_dos_dados_nao_guarda_cpf():
    item = {"cnpj": "11.111.111/0001-11", "cnpj_raiz": "11111111", "razao_social": "JOSE S SILVA ME",
            "nome_fantasia": "BARBEARIA DO ZE", "porte_empresa": {"codigo": "01"}, "matriz_filial": "MATRIZ",
            "situacao_cadastral": {"situacao_cadastral": "ATIVA"}, "data_abertura": "2019-03-01",
            "capital_social": 10000, "endereco": {"tipo_logradouro": "RUA", "logradouro": "A", "numero": "100",
            "municipio": "TERESINA", "uf": "PI", "cep": "64000000"},
            "quadro_societario": [{"nome": "JOSE", "qualificacao_socio": "Titular", "documento": "***123456**"}]}
    d = cnpj.converter_casa(item)
    assert d.cnpj == "11111111000111" and d.porte == "ME" and d.logradouro == "RUA A" and d.abertura == date(2019, 3, 1)
    assert d.socios == [{"nome": "JOSE", "qualificacao": "Titular"}]
    cnpj.completar_brasilapi(d, {"cnae_fiscal": 9602501, "opcao_pelo_mei": False, "opcao_pelo_simples": True,
                                 "ddd_telefone_1": "8632221111", "qsa": [{"nome_socio": "JOSE", "qualificacao_socio": "Titular", "cnpj_cpf_do_socio": "***"}]})
    assert d.cnae_principal == "9602501" and d.regime == "Simples" and "cnpj_cpf_do_socio" not in d.socios[0]


def test_termos_busca():
    assert cnpj.termos_busca("Barbearia do Zé LTDA") == ["barbearia", "ze"]


def test_instagram_conta_posts_30d():
    agora = datetime(2026, 10, 19, tzinfo=timezone.utc)
    item = {"username": "barbeariadoze", "followersCount": 2300, "followsCount": 300, "postsCount": 400,
            "biography": "Agende: wa.me/5586999998888", "externalUrl": "", "isBusinessAccount": True,
            "latestPosts": [{"timestamp": "2026-10-15T10:00:00Z"}, {"timestamp": "2026-10-01T10:00:00Z"},
                            {"timestamp": "2026-08-01T10:00:00Z"}]}
    p = instagram_apify.converter(item, agora)
    assert p.posts_30d == 2 and p.bio_tem_whatsapp and not p.bio_tem_link
    assert p.ultimo_post_em.date() == date(2026, 10, 15)


def test_anuncios_so_conta_a_pagina_do_negocio():
    itens = [{"page_name": "Barbearia do Zé", "page_id": "123", "start_date": 1723420800, "is_active": True,
              "ad_archive_id": "a1", "snapshot": {"body": {"text": "Promo terça"}}},
             {"page_name": "Barbearia do Zé", "page_id": "123", "start_date": 1726099200, "is_active": True,
              "ad_archive_id": "a2", "snapshot": {"body": {"text": "Corte + barba"}}},
             {"page_name": "Outra Loja Qualquer", "page_id": "999", "start_date": 1726099200, "ad_archive_id": "a3"}]
    r = anuncios_apify.resumir(itens, "Barbearia do Zé", None)
    assert r.qtd_ativos == 2 and r.pagina_id == "123" and r.mais_antigo_em == date(2024, 8, 12)
    assert len(r.amostras) == 2 and "view_all_page_id=123" in r.link_biblioteca
    vazio = anuncios_apify.resumir([itens[2]], "Barbearia do Zé", None)
    assert vazio.qtd_ativos == 0
