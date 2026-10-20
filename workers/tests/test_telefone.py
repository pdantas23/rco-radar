import pytest

from radar.telefone import normalizar, whatsapp_de_link


@pytest.mark.parametrize("bruto,e164,celular", [
    ("(86) 99999-8888", "+5586999998888", True),
    ("+55 86 99999-8888", "+5586999998888", True),
    ("5586999998888", "+5586999998888", True),
    ("086 99999-8888", "+5586999998888", True),
    ("0 21 86 99999-8888", "+5586999998888", True),   # com operadora
    ("(86) 3222-1111", "+558632221111", False),        # fixo
    ("86 9999-8888", "+5586999998888", True),          # celular antigo, sem o 9
])
def test_normaliza(bruto, e164, celular):
    tel = normalizar(bruto)
    assert tel.e164 == e164
    assert tel.celular is celular


@pytest.mark.parametrize("bruto", ["", None, "123", "(20) 99999-8888", "(86) 89999-8888", "0800 123 4567"])
def test_invalido(bruto):
    assert normalizar(bruto) is None


def test_whatsapp_de_link():
    assert whatsapp_de_link('<a href="https://wa.me/5586999998888?text=oi">') == "+5586999998888"
    assert whatsapp_de_link("https://api.whatsapp.com/send?phone=5586999998888") == "+5586999998888"
    assert whatsapp_de_link("sem link") is None
