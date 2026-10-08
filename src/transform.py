"""Normalização: de registro do índice para linhas SKU × região.

Decisões aplicadas aqui:

  * Marketplace INCLUÍDO. Todo produto entra, com o vendedor identificado em
    SELLER_PRINCIPAL / SELLER_TIPO / SELLER_ID (ver src/sellers.py). Não há
    coluna de CNPJ: não existe para vendedor de marketplace, e o da Leroy é
    constante.

  * Só regiões com UF. `outros` é descartado. Regiões novas que apareçam no
    índice e não estejam no mapa viram um aviso, não um descarte silencioso.

  * Grão da linha é SKU × REGIÃO, com a UF como coluna derivada. São Paulo tem
    8 regiões comerciais com preços independentes; agregar para UF perderia
    essa variação, então a agregação fica a critério de quem consulta.

    O grão NÃO inclui o vendedor: o índice traz um preço por região, não um
    preço por vendedor. Explodir por vendedor duplicaria o mesmo preço.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .regioes import REGIAO_NOME, regioes_desconhecidas, uf_de
from .sellers import TIPO_LEROY, identifica
from .texto import limpa

log = logging.getLogger(__name__)

COLUNAS = [
    "COD_PRODUTO",
    "EAN_PRINCIPAL",
    "EANS_TODOS",
    "DESCRICAO",
    "DESCRICAO_CURTA",
    "CATEGORIA_N1",
    "CATEGORIA_N2",
    "UNIDADE",
    "SELLER_PRINCIPAL",
    "SELLER_TIPO",
    "SELLER_ID",
    "SELLERS_TODOS",
    "QTD_SELLERS",
    "TEM_MARKETPLACE",
    "REGIAO",
    "REGIAO_NOME",
    "UF",
    "PRECO_PROMOCIONAL",
    "PRECO_ORIGINAL",
    "DISPONIVEL",
    "TEM_ESTOQUE",
    "URL",
    "EXTRAIDO_EM",
    # A resposta HTTP que originou a linha (ver evidencia/). Única coluna
    # acrescentada às 23 originais.
    "ID_EVIDENCIA",
]

# Colunas que precisam de forma canônica no hash da linha, para que o hash
# calculado na coleta seja o mesmo que a Snowflake recalcula das colunas.
TIPOS_HASH = {
    "PRECO_PROMOCIONAL": "dinheiro",
    "PRECO_ORIGINAL": "dinheiro",
    "EXTRAIDO_EM": "momento",
}


def _cat(hier: dict | None, nivel: str) -> str | None:
    """Primeiro valor de um nível da hierarquia de categorias."""
    if not isinstance(hier, dict):
        return None
    v = hier.get(nivel)
    if isinstance(v, list):
        v = v[0] if v else None
    return limpa(v)


def _dec(v) -> float | None:
    """Preço em float, tratando 0 como ausência de preço."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def stat_zerado() -> dict:
    return {
        "registros_entrada": 0,
        "produtos_leroy": 0,
        "produtos_marketplace": 0,
        "produtos_sem_seller": 0,
        "descartados_sem_regiao": 0,
        "produtos_sem_ean": 0,
        "produtos_mantidos": 0,
        "linhas_geradas": 0,
        "linhas_sem_preco": 0,
        "regioes_nao_mapeadas": [],
    }


def soma_stat(acc: dict, novo: dict) -> dict:
    for k, v in novo.items():
        if k == "regioes_nao_mapeadas":
            acc[k] = sorted(set(acc.get(k, [])) | set(v))
        else:
            acc[k] = acc.get(k, 0) + v
    return acc


def normaliza(registros: list[dict], agora: datetime | None = None,
              id_evidencia: str | None = None) -> tuple[list[dict], dict]:
    """Converte registros do índice em linhas SKU × região.

    Devolve (linhas, estatisticas). `agora` é parametrizável para que todos os
    lotes de uma mesma execução compartilhem o mesmo carimbo de tempo.

    `id_evidencia` amarra cada linha à resposta HTTP que a originou. Uma
    resposta traz até 1.000 produtos × 27 regiões, e TODAS as linhas derivadas
    dela apontam para a mesma evidência — é a mesma prova.
    """
    agora = agora or datetime.now(timezone.utc).replace(tzinfo=None)
    linhas: list[dict] = []
    chaves_vistas: set[str] = set()

    stat = stat_zerado()
    stat["registros_entrada"] = len(registros)

    for r in registros:
        sellers = (r.get("boitataFacets") or {}).get("sellers") or []
        vendedor = identifica(sellers, r.get("marketplaceSellers"))
        if vendedor["SELLER_TIPO"] == TIPO_LEROY:
            stat["produtos_leroy"] += 1
        elif vendedor["SELLER_TIPO"]:
            stat["produtos_marketplace"] += 1
        else:
            stat["produtos_sem_seller"] += 1

        regionais = r.get("regionalAttributes") or {}
        if not isinstance(regionais, dict) or not regionais:
            stat["descartados_sem_regiao"] += 1
            continue
        chaves_vistas.update(regionais)

        cod = str(r.get("product_id") or r.get("objectID") or "").strip()
        if not cod:
            continue

        # Produto sem EAN é mantido: o código Leroy identifica a linha.
        eans = [str(e) for e in (r.get("eans") or []) if e]
        if not eans:
            stat["produtos_sem_ean"] += 1

        hier = r.get("hierarchicalCategories")
        base = {
            "COD_PRODUTO": cod,
            "EAN_PRINCIPAL": eans[0] if eans else None,
            "EANS_TODOS": "|".join(eans) if eans else None,
            "DESCRICAO": limpa(r.get("name")),
            "DESCRICAO_CURTA": limpa(r.get("shortName")),
            "CATEGORIA_N1": _cat(hier, "lvl0"),
            "CATEGORIA_N2": _cat(hier, "lvl1"),
            "UNIDADE": limpa(r.get("unit")),
            **vendedor,
            "URL": limpa(r.get("url")),
            "EXTRAIDO_EM": agora,
        }

        gerou = False
        for regiao, ra in regionais.items():
            uf = uf_de(regiao)
            if uf is None:          # `outros` e quaisquer regiões sem UF
                continue
            if not isinstance(ra, dict):
                continue

            preco = _dec(ra.get("promotionalPrice"))
            if preco is None:
                stat["linhas_sem_preco"] += 1

            linha = {
                **base,
                "REGIAO": regiao,
                "REGIAO_NOME": REGIAO_NOME.get(regiao, regiao),
                "UF": uf,
                "PRECO_PROMOCIONAL": preco,
                "PRECO_ORIGINAL": _dec(ra.get("originalPrice")),
                "DISPONIVEL": bool(ra.get("available")),
                "TEM_ESTOQUE": bool((ra.get("stock") or {}).get("hasStock")),
                "ID_EVIDENCIA": id_evidencia,
            }
            linhas.append(linha)
            gerou = True

        if gerou:
            stat["produtos_mantidos"] += 1

    stat["linhas_geradas"] = len(linhas)
    # Quem chama agrega e reporta uma vez só, no fim — em streaming isso é
    # chamado centenas de vezes.
    stat["regioes_nao_mapeadas"] = sorted(regioes_desconhecidas(chaves_vistas))

    return linhas, stat
