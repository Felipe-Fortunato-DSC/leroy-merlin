"""Auditoria completa de um dia de coleta: disco, Snowflake e assinatura.

    python -m src.auditoria                     # hoje (Brasília)
    python -m src.auditoria --data 2026-10-08

Três conferências, cada uma cobrindo o que a outra não alcança:

  1. disco      cada .gz tem o hash registrado; o índice tem o hash do selo;
                a assinatura confere (evidencia.verificacao)
  2. Snowflake  a VW_AUDITORIA_EVIDENCIAS recalcula, das próprias colunas, o
                digest das linhas de cada evidência e o hash do dia
  3. selo       a linha de selo na base tem assinatura válida e é a mesma do
                disco — a Snowflake não verifica Ed25519, então é aqui
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date

from evidencia import armazenamento as ev_arm
from evidencia import snowflake as sf_evid
from evidencia import verifica_execucao
from evidencia.registro import hoje_brt
from evidencia.selo import SeloError, assinatura_confere, carrega_publica

from . import load_snowflake
from .constantes import EV_ROBO, EV_SIGLA, EVIDENCIAS


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=date.fromisoformat, default=None,
                   help="AAAA-MM-DD (padrão: hoje em Brasília)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)
    dia = args.data or hoje_brt()
    falhas = 0

    # 1. disco
    pasta = ev_arm.execucao_selada(EVIDENCIAS, EV_ROBO, dia)
    selo_disco = None
    if pasta is None:
        print(f"disco      FALHA  nenhuma execução selada de {dia} em {EVIDENCIAS}")
        falhas += 1
    else:
        r = verifica_execucao(pasta, EVIDENCIAS, EV_ROBO)
        print(f"disco      {'OK   ' if r.ok else 'FALHA'}  {r.resumo()}")
        falhas += not r.ok
        selo_disco = ev_arm.le_json(pasta / ev_arm.SELO)

    conn = load_snowflake.conecta()
    try:
        # 2. Snowflake
        linhas = sf_evid.audita(conn, load_snowflake.sql_auditoria(dia))
        status = Counter(l["STATUS"] for l in linhas)
        n_linhas = sum(l["N_LINHAS"] for l in linhas)
        ok_base = bool(linhas) and set(status) == {"integra"}
        print(f"snowflake  {'OK   ' if ok_base else 'FALHA'}  {len(linhas)} evidências, "
              f"{n_linhas} linhas: " + ", ".join(f"{v} {k}" for k, v in status.most_common()))
        for l in [l for l in linhas if l["STATUS"] != "integra"][:10]:
            print(f"           {l['ID_EVIDENCIA']}  {l['STATUS']}  ({l['N_LINHAS']} linhas)")
        falhas += not ok_base

        # 3. selo na base
        selo_base = sf_evid.selo_na_base(conn, load_snowflake.DATABASE,
                                         load_snowflake.SCHEMA, EV_SIGLA, dia)
    finally:
        conn.close()

    if selo_base is None:
        print("selo       FALHA  sem linha de selo na base")
        falhas += 1
    else:
        try:
            assinado = assinatura_confere(selo_base, carrega_publica(EVIDENCIAS, EV_ROBO))
        except SeloError as exc:
            print(f"selo       FALHA  {exc}")
            return 1
        mesmo = selo_disco is not None and selo_disco["hash_dia"] == selo_base["hash_dia"]
        ok_selo = assinado and mesmo
        print(f"selo       {'OK   ' if ok_selo else 'FALHA'}  assinatura "
              f"{'válida' if assinado else 'INVÁLIDA'}, "
              f"{'igual ao' if mesmo else 'DIFERENTE do'} selo em disco")
        falhas += not ok_selo

    print(f"\n{dia}: {'ÍNTEGRO' if not falhas else f'{falhas} FALHA(S)'}")
    return 0 if not falhas else 1


if __name__ == "__main__":
    raise SystemExit(main())
