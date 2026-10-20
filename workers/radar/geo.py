"""Divisão de área em quadrantes (F2).

Uma consulta de texto no Google Maps devolve no máximo 60 resultados. Para passar
disso, o raio da pesquisa vira uma grade de células menores, cada uma consultada
separadamente; quem aparece em várias células é deduplicado por place_id.
"""

import math
from dataclasses import dataclass

LIMITE_POR_CONSULTA = 60
KM_POR_GRAU_LAT = 111.32


@dataclass(frozen=True)
class Celula:
    lat: float
    lng: float
    raio_km: float


def grade(lat: float, lng: float, raio_km: float, max_resultados: int,
          densidade_por_km2: float | None = None, limite_por_consulta: int = LIMITE_POR_CONSULTA) -> list[Celula]:
    """Células cobrindo o círculo (lat, lng, raio_km).

    Sem densidade conhecida, cria células suficientes para que cada uma precise
    devolver menos que o limite, supondo distribuição uniforme. Com densidade
    (negócios/km² vistos numa rodada anterior), dimensiona pela estimativa real.
    """
    if raio_km <= 0:
        raise ValueError("raio_km precisa ser positivo")
    area = math.pi * raio_km ** 2
    esperado = max_resultados if densidade_por_km2 is None else min(max_resultados, densidade_por_km2 * area)
    if esperado <= limite_por_consulta:
        return [Celula(lat, lng, raio_km)]
    # margem de 30%: a distribuição nunca é uniforme
    n_celulas = math.ceil(esperado * 1.3 / limite_por_consulta)
    if n_celulas == 1:
        return [Celula(lat, lng, raio_km)]

    lado = math.ceil(math.sqrt(n_celulas))
    passo_km = 2 * raio_km / lado
    # raio da célula = meia diagonal do quadrado, para os círculos cobrirem tudo
    raio_cel = passo_km * math.sqrt(2) / 2
    km_por_grau_lng = KM_POR_GRAU_LAT * math.cos(math.radians(lat))
    celulas = []
    for i in range(lado):
        for j in range(lado):
            dy = -raio_km + passo_km * (i + 0.5)
            dx = -raio_km + passo_km * (j + 0.5)
            # descarta células cujo quadrado inteiro está fora do círculo
            h = passo_km / 2
            if math.hypot(max(abs(dx) - h, 0), max(abs(dy) - h, 0)) >= raio_km:
                continue
            celulas.append(Celula(lat + dy / KM_POR_GRAU_LAT, lng + dx / km_por_grau_lng, round(raio_cel, 3)))
    return celulas


def distancia_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
