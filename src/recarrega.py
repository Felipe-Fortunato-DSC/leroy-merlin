"""Recarrega na Snowflake a partir dos Parquets já coletados.

Útil quando a coleta deu certo mas a carga falhou — evita bater no Algolia de novo.

    python -m src.recarrega                    # coleta completa mais recente
    python -m src.recarrega --data 20261008    # um dia específico
    python -m src.recarrega --recriar

Recarrega SEMPRE o dia inteiro: a carga apaga as linhas da data antes de
inserir, então subir só uma parte apagaria as demais. Por isso a entrada é a
data, não um arquivo. As partes são lidas uma a uma — o dia completo passa de
20 milhões de linhas e não cabe num DataFrame.

As evidências e o selo do dia são repostos a partir do disco, então a base
volta a conferir na VW_AUDITORIA_EVIDENCIAS.
"""
from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd
import pyarrow.parquet as pq

from evidencia import armazenamento as ev_arm
from evidencia import snowflake as sf_evid
from evidencia.armazenamento import BRASILIA

from . import load_snowflake
from .constantes import DATA, EV_ROBO, EV_SIGLA, EVIDENCIAS, dia_mais_recente, partes_do_dia

log = logging.getLogger("recarrega")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Recarrega uma coleta já gravada em Parquet")
    p.add_argument("--data", help="AAAAMMDD da coleta (padrão: a mais recente em data/)")
    p.add_argument("--recriar", action="store_true", help="DROP + CREATE antes de carregar")
    p.add_argument("--sem-evidencia", action="store_true",
                   help="aceita Parquet anterior à camada de evidências "
                        "(a carga resultante NÃO será auditável)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    logging.getLogger("snowflake.connector").setLevel(logging.WARNING)

    dia = args.data or dia_mais_recente()
    partes = partes_do_dia(dia) if dia else []
    if not partes:
        log.error("nenhuma parte leroy_%s_pNNN.parquet em %s", dia or "*", DATA)
        return 1
    n_linhas = sum(pq.ParquetFile(a).metadata.num_rows for a in partes)
    log.info("coleta %s: %d parte(s), %d linhas", dia, len(partes), n_linhas)

    # Um Parquet sem ID_EVIDENCIA é anterior à camada de evidências. Recarregá-lo
    # povoa a base com preços que nenhuma evidência sustenta — e, pior, sem
    # aviso: as colunas entram como NULL e a tabela parece normal. Exigir a
    # flag força a decisão a ser consciente.
    faltantes = {"ID_EVIDENCIA"} - set(pq.read_schema(partes[0]).names)
    if faltantes:
        motivo = f"faltam {', '.join(sorted(faltantes))}"
    elif pd.read_parquet(partes[0], columns=["ID_EVIDENCIA"])["ID_EVIDENCIA"].isna().all():
        motivo = "ID_EVIDENCIA vazio — coletado com --sem-evidencia"
    else:
        motivo = None
    if motivo and not args.sem_evidencia:
        log.error(
            "a coleta %s não tem evidência associada (%s). Recarregar "
            "produziria linhas de preço sem prova de origem, que a auditoria "
            "rejeitaria. Rode uma coleta nova (python -m src.main), ou use "
            "--sem-evidencia se esta carga não precisa ser auditável.",
            dia, motivo,
        )
        return 1
    if motivo:
        log.warning("--sem-evidencia: esta carga NÃO será auditável")

    # O dia da carga é a data de Brasília de EXTRAIDO_EM (igual em todas as
    # linhas) — a mesma chave que a coleta usou para apagar e para as evidências.
    extraido = pd.read_parquet(partes[0], columns=["EXTRAIDO_EM"])["EXTRAIDO_EM"].iloc[0]
    dia_carga = pd.Timestamp(extraido, tz="UTC").tz_convert(BRASILIA).date()

    # Evidências da mesma execução que gerou os Parquets, para a base voltar a
    # conferir: sem elas, toda linha recarregada seria `linhas_sem_evidencia`.
    pasta = indice = selo = None
    if not motivo:
        pasta = (ev_arm.execucao_selada(EVIDENCIAS, EV_ROBO, dia_carga)
                 or next(reversed(ev_arm.execucoes(EVIDENCIAS, EV_ROBO, dia_carga)), None))
        indice = ev_arm.le_indice(pasta) if pasta else []
        primeiro_id = pd.read_parquet(partes[0], columns=["ID_EVIDENCIA"])["ID_EVIDENCIA"].iloc[0]
        if primeiro_id not in {r["id_evidencia"] for r in indice}:
            log.error("as evidências de %s (%s) não estão em %s — sem elas a carga "
                      "não confere. Use --sem-evidencia para carregar mesmo assim.",
                      primeiro_id, dia_carga, EVIDENCIAS)
            return 1
        selo = ev_arm.le_json(pasta / ev_arm.SELO)
        if selo is None:
            log.warning("a execução %s não foi selada: o dia ficará `dia_nao_selado`", pasta)

    conn = load_snowflake.conecta()
    try:
        load_snowflake.prepara_tabela(conn, dia_carga, EV_SIGLA, recriar=args.recriar)
        if indice:
            sf_evid.carrega_evidencias(conn, indice, load_snowflake.DATABASE, load_snowflake.SCHEMA)
            log.info("%d evidências de %s", len(indice), pasta)
        total = 0
        for arquivo in partes:
            total += load_snowflake.insere(conn, pd.read_parquet(arquivo))
            log.info("  %s (acumulado %d)", arquivo.name, total)
        if selo:
            sf_evid.carrega_selo(conn, selo, load_snowflake.DATABASE, load_snowflake.SCHEMA)
    finally:
        conn.close()

    log.info("carregadas %d linhas em %s.%s.%s", total, load_snowflake.DATABASE,
             load_snowflake.SCHEMA, load_snowflake.TABELA)
    if total != n_linhas:
        log.error("esperadas %d linhas, carregadas %d", n_linhas, total)
        return 1
    log.info("confira com: python -m src.auditoria --data %s", dia_carga)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
