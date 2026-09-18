"""Exporta a lista de vendedores distintos, com volume de SKUs.

A tabela não tem coluna de CNPJ — o site não expõe o CNPJ de vendedor de
marketplace (ver README). Esta planilha existe para o caso de você querer
enriquecer essa informação por fora: sai com uma coluna CNPJ em branco e
ordenada por número de SKUs, para que um preenchimento parcial cubra primeiro
quem mais pesa no catálogo.

    python -m src.vendedores                  # do Parquet mais recente
    python -m src.vendedores --snowflake      # direto da tabela
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from .sellers import LEROY_CNPJ, LEROY_RAZAO_SOCIAL, SELLER_LEROY

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"


def _do_parquet() -> pd.DataFrame:
    arquivos = sorted(DATA.glob("leroy_*.parquet"))
    if not arquivos:
        sys.exit(f"nenhum Parquet em {DATA} — rode `python -m src.main` antes")
    cols = ["SELLER_PRINCIPAL", "SELLER_TIPO", "SELLER_ID", "COD_PRODUTO"]
    bruto = pd.concat([pd.read_parquet(a, columns=cols) for a in arquivos], ignore_index=True)
    return (bruto.groupby(["SELLER_PRINCIPAL", "SELLER_TIPO"], dropna=False)
                 .agg(SELLER_ID=("SELLER_ID", "max"), QTD_SKUS=("COD_PRODUTO", "nunique"))
                 .reset_index()
                 .sort_values("QTD_SKUS", ascending=False))


def _do_snowflake() -> pd.DataFrame:
    from . import load_snowflake
    conn = load_snowflake.conecta()
    try:
        sql = f"""
            SELECT SELLER_PRINCIPAL, SELLER_TIPO, MAX(SELLER_ID) AS SELLER_ID,
                   COUNT(DISTINCT COD_PRODUTO) AS QTD_SKUS
            FROM {load_snowflake.DATABASE}.{load_snowflake.SCHEMA}.{load_snowflake.TABELA}
            WHERE SELLER_PRINCIPAL IS NOT NULL
            GROUP BY SELLER_PRINCIPAL, SELLER_TIPO
            ORDER BY QTD_SKUS DESC
        """
        return conn.cursor().execute(sql).fetch_pandas_all()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Lista de vendedores, para enriquecer CNPJ")
    p.add_argument("--snowflake", action="store_true", help="ler da tabela em vez do Parquet")
    p.add_argument("--saida", type=Path, default=DATA / "vendedores.csv")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

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
