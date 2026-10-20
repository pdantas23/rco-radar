import random

from radar.geo import LIMITE_POR_CONSULTA, distancia_km, grade


def test_poucos_resultados_uma_celula():
    cel = grade(-5.09, -42.80, 15, max_resultados=50)
    assert len(cel) == 1
    assert cel[0].raio_km == 15


def test_200_resultados_divide():
    cel = grade(-5.09, -42.80, 15, max_resultados=200)
    # 200 * 1.3 / 60 = 5 células => grade 3x3 menos as que caem fora do círculo
    assert 4 <= len(cel) <= 9
    # capacidade das células cobre o pedido
    assert len(cel) * LIMITE_POR_CONSULTA >= 200


def test_celulas_cobrem_o_circulo():
    """Todo ponto sorteado dentro do raio fica dentro de alguma célula."""
    lat, lng, raio = -5.09, -42.80, 15
    cel = grade(lat, lng, raio, max_resultados=1000)
    rnd = random.Random(42)
    for _ in range(2000):
        # ponto uniforme no círculo
        while True:
            dx, dy = rnd.uniform(-raio, raio), rnd.uniform(-raio, raio)
            if dx * dx + dy * dy <= raio * raio:
                break
        plat = lat + dy / 111.32
        plng = lng + dx / (111.32 * 0.99605)    # cos(-5.09°)
        assert any(distancia_km(plat, plng, c.lat, c.lng) <= c.raio_km + 0.05 for c in cel)


def test_densidade_reduz_celulas():
    sem = grade(-5.09, -42.80, 15, max_resultados=1000)
    com = grade(-5.09, -42.80, 15, max_resultados=1000, densidade_por_km2=0.1)  # ~70 negócios
    assert len(com) < len(sem)
