"""Orquestração da coleta Leroy Merlin.

Uso típico:
    python -m src.main --amostra 20000        # teste representativo
    python -m src.main                        # catálogo completo
    python -m src.main --parquet-only         # sem tocar na Snowflake
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import extract, transform

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"

# Linhas acumuladas antes de gravar um bloco em Parquet e inseri-lo na Snowflake.
#
# Não aumente sem medir. Cada linha em buffer é um dict de 23 campos (~2 KB), e
# um bloco de 2 milhões chegou a 4 GB de heap: a coleta degradou de 1,0 s/lote
# para 5,1 s/lote por pressão de garbage collector, cinco vezes mais lenta e
# piorando. 400 mil linhas mantêm o pico perto de 800 MB.
LINHAS_POR_BLOCO = 400_000


def configura_log(verboso: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verboso else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("snowflake.connector").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Coleta de preços Leroy Merlin por região")
    p.add_argument("--amostra", type=int, default=0,
                   help="usar N IDs sorteados do sitemap (0 = catálogo completo). "
                        "Sorteio, não os N primeiros: a ordem do sitemap é enviesada.")
    p.add_argument("--seed", type=int, default=42, help="semente do sorteio de --amostra")
    p.add_argument("--workers", type=int, default=4,
                   help="requisições simultâneas ao Algolia (padrão 4)")
    p.add_argument("--parquet-only", action="store_true",
                   help="grava o Parquet e não carrega na Snowflake")
    p.add_argument("--recriar", action="store_true",
                   help="DROP + CREATE da tabela antes de carregar")
    p.add_argument("-v", "--verboso", action="store_true")
    args = p.parse_args(argv)

    configura_log(args.verboso)
    log = logging.getLogger("main")
    DATA.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    agora = datetime.now(timezone.utc).replace(tzinfo=None)

    import pandas as pd

    # 1. canário de credenciais — falha cedo, com mensagem acionável
    extract.valida_credenciais()

    # 2. IDs do sitemap
    ids = extract.coleta_ids()
    if args.amostra and args.amostra < len(ids):
        random.seed(args.seed)
        ids = random.sample(ids, args.amostra)
        log.info("amostra aleatória de %d IDs (seed=%d)", len(ids), args.seed)

    # 3-4. coleta, normalização e carga em blocos.
    # O catálogo completo passa de 20 milhões de linhas — concatenar tudo num
    # DataFrame só consumiria dezenas de GB. Cada bloco é gravado em Parquet e
    # inserido na Snowflake, e a memória volta ao patamar anterior.
    conn = None
    dia = agora.date().isoformat()
    if not args.parquet_only:
        from . import load_snowflake
        conn = load_snowflake.conecta()
        load_snowflake.prepara_tabela(conn, dia, recriar=args.recriar)

    stat = transform.stat_zerado()
    buffer: list[dict] = []
    partes: list[Path] = []
    total_linhas = 0
    ufs: set[str] = set()
    vendedores: set[str] = set()

    def descarrega(buf: list[dict]) -> None:
        nonlocal total_linhas
        bloco = pd.DataFrame(buf, columns=transform.COLUNAS)
        buf.clear()  # solta os dicts antes do Parquet e do upload
        n_bloco = len(bloco)
        caminho = DATA / f"leroy_{agora:%Y%m%d}_p{len(partes):03d}.parquet"
        bloco.to_parquet(caminho, index=False, compression="snappy")
        partes.append(caminho)
        ufs.update(bloco["UF"].unique())
        vendedores.update(bloco["SELLER_PRINCIPAL"].dropna().unique())
        total_linhas += n_bloco
        if conn is not None:
            from . import load_snowflake
            load_snowflake.insere(conn, bloco)
        del bloco
        gc.collect()
        log.info("  bloco %d: %d linhas (acumulado %d)", len(partes), n_bloco, total_linhas)

    try:
        for lote, n, total in extract.itera_registros(ids, workers=args.workers):
            linhas, s = transform.normaliza(lote, agora=agora)
            transform.soma_stat(stat, s)
            buffer.extend(linhas)
            if len(buffer) >= LINHAS_POR_BLOCO:
                descarrega(buffer)
        if buffer:
            descarrega(buffer)
    finally:
        if conn is not None:
            conn.close()

    if not total_linhas:
        log.error("nenhuma linha gerada — abortando")
        return 1

    ausentes = len(ids) - stat["registros_entrada"]

    log.info("-" * 64)
    log.info("IDs pedidos ............... %d", len(ids))
    log.info("  sem correspondência ..... %d (%.1f%%)", ausentes, 100 * ausentes / max(len(ids), 1))
    log.info("registros recebidos ....... %d", stat["registros_entrada"])
    log.info("  Leroy Merlin ............ %d", stat["produtos_leroy"])
    log.info("  marketplace ............. %d", stat["produtos_marketplace"])
    log.info("  sem vendedor identificado %d", stat["produtos_sem_seller"])
    log.info("  descartados sem região .. %d", stat["descartados_sem_regiao"])
    log.info("  sem EAN (mantidos) ...... %d", stat["descartados_sem_ean"])
    log.info("produtos com linha ........ %d", stat["produtos_mantidos"])
    log.info("linhas SKU x região ....... %d", total_linhas)
    log.info("  sem preço ............... %d (%.1f%%)",
             stat["linhas_sem_preco"], 100 * stat["linhas_sem_preco"] / max(total_linhas, 1))
    log.info("vendedores distintos ...... %d", len(vendedores))
    log.info("UFs distintas ............. %d  %s", len(ufs), ", ".join(sorted(ufs)))
    if stat["regioes_nao_mapeadas"]:
        log.warning("REGIÕES NOVAS não mapeadas: %s — atualize src/regioes.py",
                    stat["regioes_nao_mapeadas"])
    mb = sum(p.stat().st_size for p in partes) / 1e6
    log.info("parquet ................... %d arquivo(s), %.1f MB", len(partes), mb)
    log.info("-" * 64)

    (DATA / "ultima_execucao.json").write_text(
        json.dumps({**stat, "ids_pedidos": len(ids), "ids_sem_correspondencia": ausentes,
                    "linhas": total_linhas, "vendedores": len(vendedores),
                    "ufs": sorted(ufs), "parquet": [p.name for p in partes],
                    "segundos": round(time.time() - t0, 1)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.parquet_only:
        log.info("--parquet-only: Snowflake não foi tocada")
    else:
        from . import load_snowflake
        log.info("Snowflake: %d linhas em %s.%s.%s", total_linhas,
                 load_snowflake.DATABASE, load_snowflake.SCHEMA, load_snowflake.TABELA)

    log.info("concluído em %.1fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
