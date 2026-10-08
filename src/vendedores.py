"""Exporta a lista de vendedores distintos, com volume de SKUs.

A tabela não tem coluna de CNPJ — o site não expõe o CNPJ de vendedor de
marketplace (ver README). Esta planilha existe para o caso de você querer
enriquecer essa informação por fora: sai com uma coluna CNPJ em branco e
ordenada por número de SKUs, para que um preenchimento parcial cubra primeiro
quem mais pesa no catálogo.

    python -m src.vendedores                  # da coleta mais recente em data/
    python -m src.vendedores --snowflake      # da carga mais recente na tabela
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from .constantes import DATA, dia_mais_recente, partes_do_dia
from .sellers import LEROY_CNPJ, LEROY_RAZAO_SOCIAL, SELLER_LEROY


def _do_parquet() -> pd.DataFrame:
    """Uma coleta só (a mais recente): somar dias distintos misturaria catálogos."""
    dia = dia_mais_recente()
    if dia is None:
        sys.exit(f"nenhum Parquet em {DATA} — rode `python -m src.main` antes")
    cols = ["SELLER_PRINCIPAL", "SELLER_TIPO", "SELLER_ID", "COD_PRODUTO"]
    bruto = pd.concat([pd.read_parquet(a, columns=cols) for a in partes_do_dia(dia)],
                      ignore_index=True)
    print(f"coleta de {dia}")
    return (bruto.dropna(subset=["SELLER_PRINCIPAL"])
                 .groupby(["SELLER_PRINCIPAL", "SELLER_TIPO"])
                 .agg(SELLER_ID=("SELLER_ID", "max"), QTD_SKUS=("COD_PRODUTO", "nunique"))
                 .reset_index()
                 .sort_values("QTD_SKUS", ascending=False))


def _do_snowflake() -> pd.DataFrame:
    from . import load_snowflake
    tabela = f"{load_snowflake.DATABASE}.{load_snowflake.SCHEMA}.{load_snowflake.TABELA}"
    conn = load_snowflake.conecta()
    try:
        sql = f"""
            SELECT SELLER_PRINCIPAL, SELLER_TIPO, MAX(SELLER_ID) AS SELLER_ID,
                   COUNT(DISTINCT COD_PRODUTO) AS QTD_SKUS
            FROM {tabela}
            WHERE SELLER_PRINCIPAL IS NOT NULL
              AND TO_DATE(EXTRAIDO_EM) = (SELECT MAX(TO_DATE(EXTRAIDO_EM)) FROM {tabela})
            GROUP BY SELLER_PRINCIPAL, SELLER_TIPO
            ORDER BY QTD_SKUS DESC
        """
        with conn.cursor() as cur:
            return cur.execute(sql).fetch_pandas_all()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Lista de vendedores, para enriquecer CNPJ")
    p.add_argument("--snowflake", action="store_true", help="ler da tabela em vez do Parquet")
    p.add_argument("--saida", type=Path, default=DATA / "vendedores.csv")
    args = p.parse_args(argv)

    df = _do_snowflake() if args.snowflake else _do_parquet()

    # Colunas em branco para preenchimento; a Leroy já vem resolvida.
    df["CNPJ"] = None
    df["RAZAO_SOCIAL"] = None
    leroy = df["SELLER_PRINCIPAL"] == SELLER_LEROY
    df.loc[leroy, "CNPJ"] = LEROY_CNPJ
    df.loc[leroy, "RAZAO_SOCIAL"] = LEROY_RAZAO_SOCIAL

    args.saida.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.saida, index=False, encoding="utf-8-sig")

    a_preencher = int(df["CNPJ"].isna().sum())
    print(f"vendedores distintos : {len(df)}")
    print(f"  a preencher        : {a_preencher}")
    print(f"arquivo              : {args.saida}")
    print("\ntop 15 por número de SKUs:")
    print(df.head(15).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
