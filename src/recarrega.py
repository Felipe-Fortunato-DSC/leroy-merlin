"""Recarrega na Snowflake a partir de um Parquet já coletado.

Útil quando a coleta deu certo mas a carga falhou — evita bater no Algolia de novo.

    python -m src.recarrega data/leroy_20260914.parquet --recriar
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from . import load_snowflake


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Recarrega um Parquet na Snowflake")
    p.add_argument("parquet", type=Path)
    p.add_argument("--recriar", action="store_true", help="DROP + CREATE antes de carregar")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    logging.getLogger("snowflake.connector").setLevel(logging.WARNING)
    log = logging.getLogger("recarrega")

    if not args.parquet.exists():
        log.error("arquivo não encontrado: %s", args.parquet)
        return 1

    df = pd.read_parquet(args.parquet)
    log.info("%s: %d linhas, %d SKUs", args.parquet.name, len(df), df["COD_PRODUTO"].nunique())

    n = load_snowflake.carrega(df, recriar=args.recriar)
    log.info("carregadas %d linhas em %s.%s.%s", n, load_snowflake.DATABASE,
             load_snowflake.SCHEMA, load_snowflake.TABELA)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
