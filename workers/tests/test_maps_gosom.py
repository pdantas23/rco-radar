import csv
import io
import json

import httpx
import respx

from radar.fontes.base import ConsultaMaps
from radar.fontes.maps_gosom import FonteMapsComReserva, MapsGosom, converter, zoom_para_raio
from radar.geo import grade

COLUNAS = ["input_id", "link", "title", "category", "address", "open_hours", "website", "phone", "review_count",
           "review_rating", "latitude", "longitude", "status", "place_id", "complete_address", "user_reviews", "emails"]


def linha(**kw):
    base = {"input_id": "1", "link": "https://maps/x", "title": "Barbearia do Zé", "category": "Barbearia",
            "address": "R. A, 100 - Centro, Teresina - PI, 64000-000",
            "open_hours": json.dumps({"segunda-feira": ["08:00–12:00", "14:00–18:00"]}),
            "website": "https://ze.com.br", "phone": "(86) 99999-8888", "review_count": "180", "review_rating": "4.6",
            "latitude": "-5.09", "longitude": "-42.8", "status": "", "place_id": "ChIJ1",
            "complete_address": json.dumps({"city": "Teresina", "state": "Piauí", "postal_code": "64000-000"}),
            "user_reviews": json.dumps([{"Name": "Maria", "ProfilePicture": "p", "Rating": 5,
                                         "Description": f"bom {i}", "When": "há 2 semanas"} for i in range(7)]),
            "emails": ""}
    base.update(kw)
    return base


def csv_de(linhas):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUNAS)
    w.writeheader()
    w.writerows(linhas)
    return buf.getvalue()


def test_converte_linha_do_csv():
    n = converter(linha())
    assert (n.place_id, n.estado, n.cep, n.nota, n.qtd_avaliacoes) == ("ChIJ1", "PI", "64000-000", 4.6, 180)
    assert n.horario == {"segunda-feira": "08:00–12:00, 14:00–18:00"}
    assert len(n.avaliacoes) == 5 and n.avaliacoes[0].autor == ""
    assert all("Name" not in r and "ProfilePicture" not in r for r in n.bruto["user_reviews"])
    assert not n.fechado_permanentemente
    assert converter(linha(status="Fechado permanentemente")).fechado_permanentemente
    assert converter(linha(place_id="")) is None


def test_csv_real_do_teste_na_vps():
    """CSV de verdade do gosom (busca 'barbearia', Teresina, 20/10/2026, docs/gosom-teste/d2.csv)."""
    import pathlib
    arq = pathlib.Path(__file__).parents[2] / "docs" / "gosom-teste" / "d2.csv"
    ns = [converter(l, "PI") for l in csv.DictReader(arq.open(encoding="utf-8"))]
    assert len(ns) == 20 and all(ns)
    assert all(n.estado == "PI" and n.cidade == "Teresina" and n.place_id for n in ns)
    for n in ns:
        assert len(n.avaliacoes) <= 5
        datas = [a.data for a in n.avaliacoes]
        assert all(datas) and datas == sorted(datas, reverse=True)          # as mais recentes primeiro
        assert all("Name" not in r and "ProfilePicture" not in r for r in n.bruto.get("user_reviews") or [])


def test_zoom():
    assert zoom_para_raio(1) == 16 and zoom_para_raio(4) == 14 and zoom_para_raio(100) == 10


def test_grade_respeita_limite_da_fonte():
    assert len(grade(-5.09, -42.8, 15, 50)) == 1                         # cabe nos 60 do Apify
    assert len(grade(-5.09, -42.8, 15, 50, limite_por_consulta=20)) > 1  # gosom traz ~20


@respx.mock
async def test_fluxo_do_job():
    base = "http://gosom:8080"
    criar = respx.post(f"{base}/api/v1/jobs").mock(return_value=httpx.Response(201, json={"id": "j1"}))
    respx.get(f"{base}/api/v1/jobs/j1").mock(side_effect=[
        httpx.Response(200, json={"ID": "j1", "Status": "working"}),
        httpx.Response(200, json={"ID": "j1", "Status": "ok"})])
    respx.get(f"{base}/api/v1/jobs/j1/download").mock(return_value=httpx.Response(200, text=csv_de([linha()])))
    g = MapsGosom(url=base, espera_seg=0)
    r = await g.buscar(ConsultaMaps("barbearia", -5.09, -42.8, 2.0, limite=20))
    assert [n.nome for n in r] == ["Barbearia do Zé"] and g.custo_ultima_chamada == 0
    corpo = json.loads(criar.calls[0].request.content)
    assert corpo["depth"] == 1 and corpo["lang"] == "pt" and corpo["lat"] == "-5.090000" and corpo["zoom"] == 15


class Falha:
    chave, custo_ultima_chamada, limite_por_consulta = "gosom_maps", 0.0, 20

    async def buscar(self, consulta, uf_padrao=""):
        raise RuntimeError("target closed")


class Reserva:
    chave, custo_ultima_chamada = "apify_maps", 0.0

    async def buscar(self, consulta, uf_padrao=""):
        self.custo_ultima_chamada = 0.5
        return [converter(linha())]


async def test_reserva_assume_e_custo_vai_para_ela():
    f = FonteMapsComReserva(Falha(), Reserva())
    r = await f.buscar(ConsultaMaps("barbearia", -5.09, -42.8, 2.0))
    assert len(r) == 1 and f.chave == "apify_maps" and f.custo_ultima_chamada == 0.5
    assert "target closed" in f.ultima_falha


async def test_sem_reserva_a_falha_sobe():
    import pytest
    with pytest.raises(RuntimeError):
        await FonteMapsComReserva(Falha(), None).buscar(ConsultaMaps("x", 0, 0, 1))
