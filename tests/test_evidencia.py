"""Testes da camada de evidências.

Os cenários que importam para a auditoria, cada um simulando uma forma
concreta de fraude ou falha:

  * hash estável ............. o mesmo dado produz sempre o mesmo hash
  * bruto adulterado ......... alguém edita o JSON gravado
  * índice adulterado ........ alguém muda a data, remove ou insere evidência
  * selo forjado ............. alguém recalcula o hash sem ter a chave
  * preço divergente ......... a base diz um preço que o bruto não sustenta

E as equivalências com a Snowflake, conferidas contra a Snowflake real: se
uma delas quebrar, a view acusa divergência em tudo e ninguém entende por quê.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

import pytest

from evidencia import (
    EvidenciaError, Registrador, SeloError, digest_linhas, gera_chave, hash_do_dia,
    hash_linha, parse_id, sela, sha256_bytes, verifica_execucao,
)
from evidencia import armazenamento as arm
from evidencia.registro import instante_canonico
from evidencia.selo import assinatura_confere, carrega_publica

CORPO_A = b'{"results":[{"objectID":"1","promotionalPrice":559.84}]}'
CORPO_B = b'{"results":[{"objectID":"2","promotionalPrice":100.00}]}'
T0 = datetime(2026, 10, 8, 13, 54, 0, tzinfo=timezone.utc)
DIA = date(2026, 10, 8)
ROBO = "robo_teste"


@pytest.fixture
def reg(tmp_path):
    gera_chave(tmp_path, ROBO)
    return Registrador(robo=ROBO, sigla="TS", dia=DIA, raiz=tmp_path, versao="abc123")


def _coleta(reg, corpo=CORPO_A, hashes=("h1", "h2")):
    ev = reg.registrar(corpo, "https://exemplo/api", T0, {"ids": ["1"]})
    return reg.fechar(ev, list(hashes))


def _reescreve_indice(pasta, registros):
    (pasta / arm.INDICE).write_bytes(
        b"".join(json.dumps(r).encode() + b"\n" for r in registros))


# ---------------------------------------------------------------- hash estável
def test_hash_do_bruto_e_dos_bytes(reg):
    ev = _coleta(reg)
    assert ev.hash_bruto == hashlib.sha256(CORPO_A).hexdigest()
    assert arm.le_bruto(arm.caminho_bruto(reg.pasta, ev.id_evidencia)) == CORPO_A


def test_hash_linha_estavel_e_independente_da_ordem():
    a = {"COD_PRODUTO": "1", "PRECO_PROMOCIONAL": 559.84, "UF": "MG", "DISPONIVEL": True}
    b = {"DISPONIVEL": True, "UF": "MG", "PRECO_PROMOCIONAL": 559.84, "COD_PRODUTO": "1"}
    assert hash_linha(a) == hash_linha(b)
    assert hash_linha(a) != hash_linha({**a, "PRECO_PROMOCIONAL": 559.85})


def test_dinheiro_normalizado_para_duas_casas():
    """559.8 do JSON e 559.80 do NUMBER(12,2) são o mesmo dinheiro."""
    assert hash_linha({"P": 559.8}) == hash_linha({"P": 559.80}) == hash_linha({"P": 559.800000})


def test_recusa_hash_de_texto(reg):
    with pytest.raises(TypeError):
        sha256_bytes("não é bytes")
    with pytest.raises(EvidenciaError, match="bytes"):
        reg.registrar("texto", "u")


def test_gzip_e_reproduzivel(tmp_path):
    arm.grava_bruto(tmp_path / "a.json.gz", CORPO_A)
    arm.grava_bruto(tmp_path / "b.json.gz", CORPO_A)
    assert (tmp_path / "a.json.gz").read_bytes() == (tmp_path / "b.json.gz").read_bytes()


# ---------------------------------------------------------------- IDs e pastas
def test_ids_sequenciais_e_data_da_coleta(reg):
    e1, e2 = _coleta(reg, CORPO_A), _coleta(reg, CORPO_B)
    assert (e1.id_evidencia, e2.id_evidencia) == ("EV-TS-20261008-000001", "EV-TS-20261008-000002")
    assert parse_id(e2.id_evidencia) == ("TS", DIA, 2)
    assert parse_id("EV-TS-20261008-SELO") == ("TS", DIA, None)


def test_id_usa_o_dia_da_coleta_mesmo_depois_da_meia_noite(reg):
    """Uma execução que atravessa a meia-noite continua sendo a carga do dia
    em que começou: ID, pasta e selo não se partem em dois."""
    ev = reg.registrar(CORPO_A, "u", datetime(2026, 10, 9, 3, 30, tzinfo=timezone.utc))
    assert ev.id_evidencia.startswith("EV-TS-20261008-")
    assert ev.dt_requisicao == "2026-10-09T03:30:00.000Z"


def test_nova_execucao_no_mesmo_dia_tem_pasta_propria_e_nao_reusa_ids(reg, tmp_path):
    _coleta(reg)
    reg2 = Registrador(robo=ROBO, sigla="TS", dia=DIA, raiz=tmp_path, versao="abc123")
    assert reg2.pasta != reg.pasta
    assert _coleta(reg2).id_evidencia == "EV-TS-20261008-000002"


def test_bruto_sem_indice_ainda_ocupa_o_numero(reg, tmp_path):
    """Processo morto entre gravar o .gz e indexar: o número não é reaproveitado."""
    reg.registrar(CORPO_A, "u", T0)                     # nunca fechado
    reg2 = Registrador(robo=ROBO, sigla="TS", dia=DIA, raiz=tmp_path, versao="abc123")
    assert _coleta(reg2).id_evidencia.endswith("-000002")


def test_instante_canonico_tem_milissegundos_e_utc():
    brt = datetime.fromisoformat("2026-10-08T16:01:38.106480-03:00")
    assert instante_canonico(brt) == "2026-10-08T19:01:38.106Z"


# ---------------------------------------------------------------- selo e adulteração
def test_execucao_integra_passa(reg, tmp_path):
    _coleta(reg, CORPO_A)
    _coleta(reg, CORPO_B)
    selo = sela(reg.pasta, tmp_path, ROBO)
    assert selo["id_evidencia"] == "EV-TS-20261008-SELO" and selo["n_evidencias"] == 2
    r = verifica_execucao(reg.pasta, tmp_path, ROBO)
    assert r.ok, r.problemas


def test_bruto_adulterado_e_detectado(reg, tmp_path):
    ev = _coleta(reg)
    sela(reg.pasta, tmp_path, ROBO)
    arm.grava_bruto(arm.caminho_bruto(reg.pasta, ev.id_evidencia),
                    CORPO_A.replace(b"559.84", b"459.84"))
    r = verifica_execucao(reg.pasta, tmp_path, ROBO)
    assert r.problemas == [f"{ev.id_evidencia}: bruto alterado"]


def test_bruto_removido_e_detectado(reg, tmp_path):
    ev = _coleta(reg)
    sela(reg.pasta, tmp_path, ROBO)
    arm.caminho_bruto(reg.pasta, ev.id_evidencia).unlink()
    assert verifica_execucao(reg.pasta, tmp_path, ROBO).problemas == [
        f"{ev.id_evidencia}: bruto ausente"]


@pytest.mark.parametrize("adultera", [
    lambda rs: [{**rs[0], "dt_requisicao": "2026-10-07T13:54:00.000Z"}, rs[1], rs[2]],  # data
    lambda rs: [rs[0], rs[2]],                                                       # remoção
    lambda rs: rs[:2],                                                               # a última
    lambda rs: [{**rs[1], "digest_linhas": "0" * 64}, rs[0], rs[2]],                 # digest
])
def test_indice_adulterado_e_detectado(reg, tmp_path, adultera):
    for corpo in (CORPO_A, CORPO_B, b'{"results":[]}'):
        _coleta(reg, corpo)
    sela(reg.pasta, tmp_path, ROBO)
    _reescreve_indice(reg.pasta, adultera(arm.le_indice(reg.pasta)))
    r = verifica_execucao(reg.pasta, tmp_path, ROBO)
    assert "índice alterado: o hash do dia não é o do selo" in r.problemas


def test_selo_recalculado_sem_a_chave_nao_confere(reg, tmp_path):
    """Quem adultera o índice e recalcula o hash do dia ainda precisa da chave."""
    _coleta(reg)
    selo = sela(reg.pasta, tmp_path, ROBO)
    pub = carrega_publica(tmp_path, ROBO)
    assert assinatura_confere(selo, pub)
    assert not assinatura_confere({**selo, "hash_dia": "0" * 64}, pub)
    assert not assinatura_confere({**selo, "id_evidencia": "EV-TS-20261009-SELO"}, pub)


def test_execucao_sem_selo_nao_e_integra(reg, tmp_path):
    _coleta(reg)
    assert verifica_execucao(reg.pasta, tmp_path, ROBO).problemas == ["sem selo"]


def test_selo_recusa_execucao_vazia(reg, tmp_path):
    with pytest.raises(SeloError, match="nenhuma evidência"):
        sela(reg.pasta, tmp_path, ROBO)


def test_chave_nao_e_sobrescrita_por_acidente(reg, tmp_path):
    with pytest.raises(SeloError, match="já existe chave"):
        gera_chave(tmp_path, ROBO)


# ---------------------------------------------------------------- preço divergente
REGISTRO_ALGOLIA = {
    "results": [{
        "objectID": "1571208340",
        "product_id": "1571208340",
        "name": "Cortina Persiana Rolo Tela Solar",
        "shortName": "Cortina Persiana Rolo",
        "eans": ["7909761247774"],
        "unit": "cada",
        "url": "https://www.leroymerlin.com.br/cortina_1571208340",
        "boitataFacets": {"sellers": ["Arelle Decor"]},
        "marketplaceSellers": ["4321"],
        "hierarchicalCategories": {"lvl0": ["Decoracao"], "lvl1": ["Decoracao > Cortinas"]},
        "regionalAttributes": {
            "uberlandia": {"promotionalPrice": 559.84, "originalPrice": 0,
                           "available": True, "stock": {"hasStock": True}},
            "rio_de_janeiro": {"promotionalPrice": 559.84, "originalPrice": 0,
                               "available": True, "stock": {"hasStock": True}},
            "outros": {"promotionalPrice": 559.84, "originalPrice": 0},
        },
    }]
}
AGORA = datetime(2026, 10, 8, 16, 54, 0)


def test_preco_divergente_e_detectado_pelo_digest(reg):
    """Reextrair do BRUTO gravado dá as mesmas linhas; mudar um preço muda o digest."""
    from src import transform

    corpo = json.dumps(REGISTRO_ALGOLIA).encode()
    ev = reg.registrar(corpo, "u", T0)
    linhas, _ = transform.normaliza(REGISTRO_ALGOLIA["results"], agora=AGORA,
                                    id_evidencia=ev.id_evidencia)
    ev = reg.fechar(ev, [hash_linha(l) for l in linhas])
    assert len(linhas) == 2                                       # `outros` descartada
    assert {l["ID_EVIDENCIA"] for l in linhas} == {ev.id_evidencia}

    bruto = json.loads(arm.le_bruto(arm.caminho_bruto(reg.pasta, ev.id_evidencia)))
    refeitas, _ = transform.normaliza(bruto["results"], agora=AGORA,
                                      id_evidencia=ev.id_evidencia)
    assert digest_linhas(hash_linha(l) for l in refeitas) == ev.digest_linhas

    adulteradas = [{**linhas[0], "PRECO_PROMOCIONAL": 459.84}, linhas[1]]
    assert digest_linhas(hash_linha(l) for l in adulteradas) != ev.digest_linhas
    assert digest_linhas([hash_linha(linhas[0])]) != ev.digest_linhas      # DELETE


def test_linha_repontada_para_outra_evidencia_muda_o_hash():
    from src import transform
    l1, _ = transform.normaliza(REGISTRO_ALGOLIA["results"], agora=AGORA, id_evidencia="EV-1")
    l2, _ = transform.normaliza(REGISTRO_ALGOLIA["results"], agora=AGORA, id_evidencia="EV-2")
    assert hash_linha(l1[0]) != hash_linha(l2[0])


# ---------------------------------------------------------------- paridade com a Snowflake
def test_digest_equivale_a_formula_da_snowflake():
    """SHA2(LISTAGG(h, '') WITHIN GROUP (ORDER BY h), 256) sobre ['bb','aa','cc']."""
    assert digest_linhas(["bb", "aa", "cc"]).startswith("a5b432ee0307be7f")
    assert digest_linhas([]) == hashlib.sha256(b"").hexdigest()     # = SHA2('', 256)


def test_hash_do_dia_equivale_a_formula_da_snowflake():
    """Conferido na Snowflake com evidencia.snowflake._sql_hash_dia: horários
    gravados em -03:00, milissegundos, ordem por ID e '|' dentro do endpoint."""
    evs = [
        {"id_evidencia": "EV-TS-20261008-000002", "dt_requisicao": "2026-10-08T23:30:00.250Z",
         "endpoint": "https://exemplo/api?x=1|2", "hash_bruto": "b" * 64, "digest_linhas": "d" * 64},
        {"id_evidencia": "EV-TS-20261008-000001", "dt_requisicao": "2026-10-08T13:54:00.000Z",
         "endpoint": "https://exemplo/api", "hash_bruto": "a" * 64, "digest_linhas": "c" * 64},
    ]
    assert hash_do_dia(evs) == "9fd2185fb33405d54b430073b0b4de5d6f5dcdec4d0a43b6d2e04ec665ba4cb2"
