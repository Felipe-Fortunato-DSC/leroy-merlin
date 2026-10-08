"""Orquestração da coleta Leroy Merlin.

Uso típico:
    python -m src.main --amostra 30000        # teste representativo
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

import pandas as pd

from evidencia import Evidencia, Registrador, SeloError, hash_do_dia, hash_linha, sela
from evidencia import armazenamento as ev_arm
from evidencia import snowflake as sf_evid
from evidencia.armazenamento import BRASILIA

from . import extract, load_snowflake, transform
from .constantes import DATA, EV_ROBO, EV_SIGLA, EVIDENCIAS, nome_parte

log = logging.getLogger("main")

# Linhas acumuladas antes de gravar um bloco em Parquet e inseri-lo na Snowflake.
#
# Não aumente sem medir. Cada linha em buffer é um dict de 24 campos (~2 KB), e
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


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
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
    p.add_argument("--sem-evidencia", action="store_true",
                   help="não grava evidências (apenas diagnóstico — a carga "
                        "resultante NÃO é auditável)")
    p.add_argument("--nao-selar", action="store_true",
                   help="não assina o selo do dia ao fim da execução")
    p.add_argument("--evidencias", type=Path, default=EVIDENCIAS,
                   help=f"diretório raiz das evidências (padrão: {EVIDENCIAS})")
    p.add_argument("-v", "--verboso", action="store_true")
    return p.parse_args(argv)


class Carga:
    """Acumula linhas e as descarrega em blocos: Parquet e, se houver, Snowflake.

    O catálogo completo passa de 20 milhões de linhas — concatenar tudo num
    DataFrame só consumiria dezenas de GB. Cada bloco é gravado e enviado, e a
    memória volta ao patamar anterior.
    """

    def __init__(self, dia: str, conn=None, amostra: bool = False) -> None:
        self.dia = dia                    # AAAAMMDD, para o nome dos arquivos
        self.conn = conn
        self.amostra = amostra
        self.buffer: list[dict] = []
        self.partes: list[Path] = []
        self.total_linhas = 0
        self.ufs: set[str] = set()
        self.vendedores: set[str] = set()
        # Evidências ainda não enviadas. Só acumulam quando há Snowflake: no
        # --parquet-only ficam apenas no disco, que é a prova.
        self._evidencias: list[Evidencia] = []

    def evidencia(self, ev: Evidencia) -> None:
        if self.conn is not None:
            self._evidencias.append(ev)

    def adiciona(self, linhas: list[dict]) -> None:
        self.buffer.extend(linhas)
        if len(self.buffer) >= LINHAS_POR_BLOCO:
            self._descarrega()

    def finaliza(self) -> None:
        if self.buffer:
            self._descarrega()
        # Sobra quando os últimos lotes não geraram linha nenhuma.
        self._envia_evidencias()
        self._remove_partes_antigas()

    def _envia_evidencias(self) -> None:
        if self.conn is not None and self._evidencias:
            sf_evid.carrega_evidencias(self.conn, self._evidencias,
                                       load_snowflake.DATABASE, load_snowflake.SCHEMA)
            self._evidencias.clear()

    def _descarrega(self) -> None:
        bloco = pd.DataFrame(self.buffer, columns=transform.COLUNAS)
        self.buffer.clear()  # solta os dicts antes do Parquet e do upload
        caminho = DATA / nome_parte(self.dia, len(self.partes), self.amostra)
        bloco.to_parquet(caminho, index=False, compression="snappy")
        self.partes.append(caminho)
        self.ufs.update(bloco["UF"].unique())
        self.vendedores.update(bloco["SELLER_PRINCIPAL"].dropna().unique())
        self.total_linhas += len(bloco)
        if self.conn is not None:
            # Evidências ANTES dos preços, também na Snowflake: em qualquer
            # instante, toda linha de preço na base tem a sua evidência lá.
            self._envia_evidencias()
            load_snowflake.insere(self.conn, bloco)
        log.info("  bloco %d: %d linhas (acumulado %d)",
                 len(self.partes), len(bloco), self.total_linhas)
        del bloco
        gc.collect()

    def _remove_partes_antigas(self) -> None:
        """Apaga partes de uma execução anterior do mesmo dia que esta não reescreveu.

        Sem isso, rodar de novo com menos blocos deixaria arquivos velhos
        misturados aos novos — e o recarrega/comprovante leriam os dois.
        """
        n = len(self.partes)
        while (velha := DATA / nome_parte(self.dia, n, self.amostra)).exists():
            velha.unlink()
            log.info("  removida parte antiga %s", velha.name)
            n += 1


def _relatorio(stat: dict, ids: list[str], carga: Carga, n_evidencias: int) -> int:
    ausentes = len(ids) - stat["registros_entrada"]
    total = carga.total_linhas
    log.info("-" * 64)
    log.info("IDs pedidos ............... %d", len(ids))
    log.info("  sem correspondência ..... %d (%.1f%%)", ausentes, 100 * ausentes / max(len(ids), 1))
    log.info("registros recebidos ....... %d", stat["registros_entrada"])
    log.info("  Leroy Merlin ............ %d", stat["produtos_leroy"])
    log.info("  marketplace ............. %d", stat["produtos_marketplace"])
    log.info("  sem vendedor identificado %d", stat["produtos_sem_seller"])
    log.info("  descartados sem região .. %d", stat["descartados_sem_regiao"])
    log.info("  sem EAN (mantidos) ...... %d", stat["produtos_sem_ean"])
    log.info("produtos com linha ........ %d", stat["produtos_mantidos"])
    log.info("linhas SKU x região ....... %d", total)
    log.info("  sem preço ............... %d (%.1f%%)",
             stat["linhas_sem_preco"], 100 * stat["linhas_sem_preco"] / max(total, 1))
    log.info("vendedores distintos ...... %d", len(carga.vendedores))
    log.info("UFs distintas ............. %d  %s", len(carga.ufs), ", ".join(sorted(carga.ufs)))
    if stat["regioes_nao_mapeadas"]:
        log.warning("REGIÕES NOVAS não mapeadas: %s — atualize src/regioes.py",
                    stat["regioes_nao_mapeadas"])
    mb = sum(p.stat().st_size for p in carga.partes) / 1e6
    log.info("parquet ................... %d arquivo(s), %.1f MB", len(carga.partes), mb)
    log.info("evidencias registradas .... %d", n_evidencias)
    log.info("-" * 64)
    return ausentes


def _sela(registrador: Registrador, conn) -> tuple[bool | None, dict | None]:
    """Confere a carga contra as evidências e, se bater, assina o dia.

    Só depois de a carga terminar: um selo no meio não cobriria o dia inteiro.

    A conferência é o portão do selo. Não procura fraude: procura erro nosso
    entre o que foi coletado e o que de fato ficou gravado. Se não bater, o dia
    NÃO é selado — "selado" significa "coletado, carregado e conferido", e um
    dia sem selo aparece como `dia_nao_selado` na view, sem ninguém ler log.
    """
    dia = registrador.dia
    conferencia_ok = None
    if conn is not None:
        # 1. cada evidência: o digest recalculado das linhas na base confere?
        linhas = sf_evid.audita(conn, load_snowflake.sql_auditoria(dia))
        problemas = [l for l in linhas if l["STATUS"] != "dia_nao_selado"]
        # 2. o conjunto: o hash do dia recalculado na base é o do disco?
        local = hash_do_dia(ev_arm.le_indice(registrador.pasta))
        base = sf_evid.hash_dia_na_base(conn, load_snowflake.DATABASE,
                                        load_snowflake.SCHEMA, EV_SIGLA, dia)
        conferencia_ok = not problemas and local == base
        if not conferencia_ok:
            for l in problemas[:10]:
                log.error("  %s  %s  (%d linhas)", l["ID_EVIDENCIA"], l["STATUS"], l["N_LINHAS"])
            if local != base:
                log.error("  hash do dia: disco %s, base %s", local[:16], (base or "-")[:16])
            log.error("SELO NÃO EMITIDO: a base não confere com as evidências. "
                      "A carga é idempotente por dia — corrija e rode de novo.")
            return False, None
        log.info("conferência OK: %d evidências, base e disco com o mesmo hash do dia",
                 len(linhas))
    else:
        log.info("conferência com a base pulada (--parquet-only)")

    try:
        selo = sela(registrador.pasta, registrador.raiz, registrador.robo)
    except SeloError as exc:
        # Falta de chave não invalida a coleta: as evidências estão gravadas e
        # podem ser seladas depois com `python -m evidencia selar`. Mas tem de
        # aparecer alto no log.
        log.error("SELO NÃO GERADO: %s", exc)
        return conferencia_ok, None
    if conn is not None:
        sf_evid.carrega_selo(conn, selo, load_snowflake.DATABASE, load_snowflake.SCHEMA)
    return conferencia_ok, selo


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    configura_log(args.verboso)
    DATA.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    inicio = datetime.now(timezone.utc)
    # EXTRAIDO_EM é o início da execução, em UTC e sem fuso — igual para todas
    # as linhas. O horário real de cada preço está na evidência.
    agora = inicio.replace(tzinfo=None)
    # O dia da coleta é a data de Brasília do início: une o ID das evidências,
    # a pasta, o selo e a carga do dia na Snowflake, mesmo que a execução
    # atravesse a meia-noite.
    dia = inicio.astimezone(BRASILIA).date()

    # 1. canário de credenciais — falha cedo, com mensagem acionável
    extract.valida_credenciais()

    # 2. registrador de evidências. Criado ANTES da primeira requisição: se o
    # diretório estiver errado, é melhor falhar agora do que depois de coletar
    # 20 milhões de linhas sem prova.
    registrador = None
    if args.sem_evidencia:
        log.warning("--sem-evidencia: esta carga NÃO será auditável")
    else:
        registrador = Registrador(robo=EV_ROBO, sigla=EV_SIGLA, dia=dia, raiz=args.evidencias)
        log.info("evidências em %s (versão do código: %s)",
                 registrador.pasta, registrador.versao)

    # 3. IDs do sitemap. Sitemaps não produzem linha de preço, mas são
    # evidência: definem QUAIS produtos foram pedidos.
    ev_sitemaps: list[Evidencia] = []

    def registra_sitemap(resp: extract.RespostaBruta) -> None:
        ev = registrador.registrar(resp.corpo, resp.endpoint, resp.dt_requisicao,
                                   resp.parametros, resp.servidor_date)
        ev_sitemaps.append(registrador.fechar(ev, []))

    ids = extract.coleta_ids(ao_receber=registra_sitemap if registrador else None)
    amostra = 0 < args.amostra < len(ids)
    if amostra:
        ids = random.Random(args.seed).sample(ids, args.amostra)
        log.info("amostra aleatória de %d IDs (seed=%d)", len(ids), args.seed)

    conn = None
    if not args.parquet_only:
        conn = load_snowflake.conecta()
    try:
        if conn is not None:
            load_snowflake.prepara_tabela(conn, dia, EV_SIGLA, recriar=args.recriar)

        carga = Carga(f"{dia:%Y%m%d}", conn, amostra=amostra)
        stat = transform.stat_zerado()
        for ev in ev_sitemaps:
            carga.evidencia(ev)
        n_evidencias = len(ev_sitemaps)

        # 4. coleta, normalização e carga em blocos
        for resp, _, _ in extract.itera_registros(ids, workers=args.workers):
            # ORDEM OBRIGATÓRIA: o bruto vai para o disco antes de o primeiro
            # preço ser derivado dele. Se o processo morrer aqui, sobra um .gz
            # sem preço — nunca um preço sem prova.
            ev = None
            if registrador is not None:
                ev = registrador.registrar(resp.corpo, resp.endpoint, resp.dt_requisicao,
                                           resp.parametros, resp.servidor_date)
            resp.libera()               # solta os bytes: já estão no disco

            linhas, s = transform.normaliza(
                resp.registros, agora=agora,
                id_evidencia=ev.id_evidencia if ev else None)
            if ev is not None:
                carga.evidencia(registrador.fechar(ev, [hash_linha(l) for l in linhas]))
                n_evidencias += 1
            transform.soma_stat(stat, s)
            carga.adiciona(linhas)
        carga.finaliza()

        if not carga.total_linhas:
            log.error("nenhuma linha gerada — abortando")
            return 1

        ausentes = _relatorio(stat, ids, carga, n_evidencias)

        # 5. conferência e selo do dia
        conferencia_ok, selo = None, None
        if registrador is not None and not args.nao_selar:
            conferencia_ok, selo = _sela(registrador, conn)
            log.info("-" * 64)
    finally:
        if conn is not None:
            conn.close()

    (DATA / "ultima_execucao.json").write_text(
        json.dumps({**stat, "dia": dia.isoformat(), "ids_pedidos": len(ids),
                    "ids_sem_correspondencia": ausentes,
                    "linhas": carga.total_linhas, "vendedores": len(carga.vendedores),
                    "ufs": sorted(carga.ufs), "parquet": [p.name for p in carga.partes],
                    "evidencias": n_evidencias,
                    "pasta_evidencias": str(registrador.pasta) if registrador else None,
                    "conferencia": conferencia_ok,
                    "selo": {k: selo[k] for k in ("id_evidencia", "hash_dia", "assinatura")}
                            if selo else None,
                    "segundos": round(time.time() - t0, 1)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.parquet_only:
        log.info("--parquet-only: Snowflake não foi tocada")
    else:
        log.info("Snowflake: %d linhas em %s.%s.%s", carga.total_linhas,
                 load_snowflake.DATABASE, load_snowflake.SCHEMA, load_snowflake.TABELA)
    log.info("concluído em %.1fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
