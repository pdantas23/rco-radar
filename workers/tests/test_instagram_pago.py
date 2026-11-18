"""Gate da pesquisa "Completa": só filtros.instagram_pago = true (booleano) libera Instagram pago e anúncios."""

from radar import etapas


def test_so_o_booleano_true_liga():
    assert etapas.instagram_pago_nos_filtros({"instagram_pago": True}) is True
    assert etapas.instagram_pago_nos_filtros({"max_resultados": 200, "instagram_pago": True}) is True


def test_qualquer_outra_coisa_e_gratis():
    for filtros in (None, {}, {"max_resultados": 200}, {"instagram_pago": False}, {"instagram_pago": None},
                    {"instagram_pago": "true"}, {"instagram_pago": 1}, [], "instagram_pago"):
        assert etapas.instagram_pago_nos_filtros(filtros) is False, filtros


class ConnFalsa:
    """Responde ao select de filtros da pesquisa sem banco."""

    def __init__(self, filtros, existe=True):
        self.filtros, self.existe, self.consultas = filtros, existe, 0

    def execute(self, sql, params):
        self.consultas += 1
        assert "select filtros from radar.pesquisas" in sql
        linha = (self.filtros,) if self.existe else None
        return type("R", (), {"fetchone": lambda _self: linha})()


def test_sem_pesquisa_acao_manual_segue_liberada_sem_consultar():
    c = ConnFalsa({})
    assert etapas.instagram_pago(c, None) is True
    assert c.consultas == 0


def test_com_pesquisa_le_os_filtros():
    assert etapas.instagram_pago(ConnFalsa({"instagram_pago": True}), "p1") is True
    assert etapas.instagram_pago(ConnFalsa({"max_resultados": 50}), "p1") is False
    assert etapas.instagram_pago(ConnFalsa(None), "p1") is False
    assert etapas.instagram_pago(ConnFalsa({"instagram_pago": True}, existe=False), "p1") is False


def test_falha_do_maps_proprio_so_oferece_tentar_de_novo():
    msg = etapas.FalhaDoMapsProprio.MENSAGEM.format(detalhe="x")
    assert "tentar de novo" in msg and "Apify" not in msg and "API" not in msg
    assert "—" not in msg and "–" not in msg
