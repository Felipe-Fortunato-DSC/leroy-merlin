"""Testes do lado da coleta (src/)."""
from __future__ import annotations

from src.constantes import dia_mais_recente, nome_parte, partes_do_dia
from src.sellers import TIPO_LEROY, TIPO_MARKETPLACE, identifica
from src.texto import limpa


# ------------------------------------------------------ texto e vendedores
def test_limpa_remove_controles_c0_e_c1():
    assert limpa("Kit Gamer Skill\x7f\x7f") == "Kit Gamer Skill"
    assert limpa("a\tb\x9f") == "ab"
    assert limpa("  ") is None
    assert limpa(None) is None
    assert limpa(123) == "123"


def test_leroy_e_a_vendedora_principal_quando_presente():
    v = identifica(["Tubrax", "Leroy Merlin"], ["99"])
    assert v["SELLER_PRINCIPAL"] == "Leroy Merlin" and v["SELLER_TIPO"] == TIPO_LEROY
    assert v["TEM_MARKETPLACE"] and v["QTD_SELLERS"] == 2

    v = identifica(["Tubrax\x7f"], None)
    assert v["SELLER_PRINCIPAL"] == "Tubrax" and v["SELLER_TIPO"] == TIPO_MARKETPLACE

    v = identifica([], [])
    assert v["SELLER_PRINCIPAL"] is None and v["QTD_SELLERS"] == 0


# ------------------------------------------------------ partes do Parquet
def test_partes_do_dia_ignoram_amostras_e_outros_dias(tmp_path):
    for nome in (nome_parte("20261008", 1), nome_parte("20261008", 0),
                 nome_parte("20261008", 0, amostra=True), nome_parte("20260914", 0),
                 "leroy_20261008.parquet"):
        (tmp_path / nome).touch()

    assert [p.name for p in partes_do_dia("20261008", tmp_path)] == [
        "leroy_20261008_p000.parquet", "leroy_20261008_p001.parquet"]
    assert dia_mais_recente(tmp_path) == "20261008"
    assert dia_mais_recente(tmp_path / "vazio") is None
