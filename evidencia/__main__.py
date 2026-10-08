"""CLI do pacote de evidências (lado do disco).

    python -m evidencia chave     --robo leroy_merlin_precos
    python -m evidencia selar     --robo leroy_merlin_precos [--data 2026-10-08]
    python -m evidencia verificar --robo leroy_merlin_precos [--data 2026-10-08]

`--data` padrão: hoje em Brasília. `selar` e `verificar` usam a última
execução do dia (a pasta mais recente).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from . import armazenamento as arm
from .registro import hoje_brt
from .selo import gera_chave, sela
from .verificacao import verifica_execucao


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evidencia", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--raiz", type=Path, default=arm.RAIZ_PADRAO,
                   help="diretório das evidências (padrão: evidencias/)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("chave", help="gera o par de chaves Ed25519 do robô")
    c.add_argument("--robo", required=True)
    c.add_argument("--sobrescrever", action="store_true",
                   help="CUIDADO: invalida a verificação dos selos já assinados")
    for nome, ajuda in (("selar", "assina a última execução do dia"),
                        ("verificar", "confere brutos, hash do dia e assinatura")):
        s = sub.add_parser(nome, help=ajuda)
        s.add_argument("--robo", required=True)
        s.add_argument("--data", type=date.fromisoformat, default=None,
                       help="AAAA-MM-DD (padrão: hoje em Brasília)")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    if a.cmd == "chave":
        caminho = gera_chave(a.raiz, a.robo, sobrescrever=a.sobrescrever)
        print(f"chave privada: {caminho}")
        print(f"chave pública: {arm.caminho_chave(a.raiz, a.robo, publica=True)}")
        print("\nEm produção, guarde a privada FORA do diretório de evidências "
              "(variável EVIDENCIA_CHAVES): se quem altera a evidência também "
              "alcança a chave, o selo não prova nada.")
        return 0

    dia = a.data or hoje_brt()
    execs = arm.execucoes(a.raiz, a.robo, dia)
    if not execs:
        print(f"nenhuma execução de {a.robo} em {dia}", file=sys.stderr)
        return 1
    pasta = execs[-1] if a.cmd == "selar" else (
        arm.execucao_selada(a.raiz, a.robo, dia) or execs[-1])

    if a.cmd == "selar":
        selo = sela(pasta, a.raiz, a.robo)
        print(f"{selo['id_evidencia']}: {selo['n_evidencias']} evidências, "
              f"hash do dia {selo['hash_dia']}")
        return 0

    r = verifica_execucao(pasta, a.raiz, a.robo)
    print(r.resumo())
    for prob in r.problemas[5:25]:
        print(f"  {prob}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
