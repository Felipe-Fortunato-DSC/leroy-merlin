"""Comprovante de coleta de um preco, com verificacao de integridade.

E a prova que vai para o auditor. Cada campo tem procedencia declarada:

    Codigo / EAN / descricao / preco / vendedor / regiao  -> base de precos
    Coletado em                                           -> evidencia (hora real
                                                             da requisicao, em
                                                             Brasilia)
    Integridade                                           -> a linha reextraida
                                                             do JSON bruto e
                                                             identica a gravada

A hora NAO sai de EXTRAIDO_EM. Aquele campo e o inicio da execucao e vale para
todas as linhas da carga; numa coleta de 17 minutos, a ultima linha estaria
afirmando um horario 40 minutos adiantado. DT_REQUISICAO e o instante da
requisicao que trouxe aquele preco.

Uso local (a partir do Parquet e das evidencias em disco):

    python -m src.comprovante --produto 92236424 --regiao uberlandia
    python -m src.comprovante --produto 92236424 --data 20261008

No painel, a mesma juncao e TBL_LEROY_TESTE x TBL_EVIDENCIAS_TESTE por
ID_EVIDENCIA, com o STATUS da VW_AUDITORIA_EVIDENCIAS.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from evidencia import armazenamento as arm
from evidencia import hash_linha, parse_id, verifica_execucao
from evidencia.armazenamento import BRASILIA

from . import transform
from .constantes import DATA, EV_ROBO, EVIDENCIAS, dia_mais_recente, partes_do_dia


def _vazio(v) -> bool:
    """None e NaN sao a mesma coisa aqui.

    O Parquet devolve NaN onde a coluna e nula, e NaN falha silenciosamente em
    qualquer comparacao: `NaN > x` e False, entao sem este teste o comprovante
    classificaria um preco sem promocao como se tivesse preco original.
    """
    return v is None or (isinstance(v, float) and v != v)


def _brl(v) -> str:
    if _vazio(v):
        return "nao informado"
    return f"R$ {v:,.2f}".replace(",", "~").replace(".", ",").replace("~", ".")


def _observacao(promo, original) -> str:
    """Explica o preco em uma linha, sem inventar o que a fonte nao disse."""
    if _vazio(promo):
        return "sem preco na fonte"
    if _vazio(original):
        return "sem promocao ativa; a fonte nao informou preco original"
    if original > promo:
        return f"preco a vista/promocional; preco original {_brl(original)}"
    return "preco unico, sem desconto"


def monta(linha: dict, evidencia: dict, integra: bool | None = None) -> str:
    dt = datetime.fromisoformat(evidencia["dt_requisicao"]).astimezone(BRASILIA)
    selo = "verificada" if integra else ("NAO CONFERE" if integra is False else "nao verificada")
    marca = "OK" if integra else ("FALHA" if integra is False else "?")
    return "\n".join([
        "Comprovante de Coleta - Leroy Merlin",
        f"{linha['DESCRICAO']}",
        f"Codigo {linha['COD_PRODUTO']} | EAN {linha['EAN_PRINCIPAL'] or 'nao informado'}",
        f"Preco: {_brl(linha['PRECO_PROMOCIONAL'])} "
        f"({_observacao(linha['PRECO_PROMOCIONAL'], linha['PRECO_ORIGINAL'])})",
        f"Vendido por: {linha['SELLER_PRINCIPAL'] or 'nao identificado'} "
        f"({(linha['SELLER_TIPO'] or '?').lower()}) "
        f"| Regiao: {linha['REGIAO_NOME']}/{linha['UF']}",
        f"Coletado em: {dt:%d/%m/%Y} as {dt:%H:%M} (Brasilia)",
        f"Origem: {linha['URL']}",
        f"Integridade: [{marca}] {selo} | Evidencia {linha['ID_EVIDENCIA']}",
    ])


def _evidencia(raiz: Path, id_evidencia: str) -> tuple[Path, dict] | None:
    """Pasta da execucao e registro da evidencia - a data vem do proprio ID."""
    _, dia, _ = parse_id(id_evidencia)
    for pasta in reversed(arm.execucoes(raiz, EV_ROBO, dia)):
        for e in arm.le_indice(pasta):
            if e["id_evidencia"] == id_evidencia:
                return pasta, e
    return None


def _linhas(dia: str, regiao: str, produto: str | None) -> pd.DataFrame:
    """Linhas da regiao (e do produto) na coleta de `dia`, em todas as partes.

    Um produto pode estar em qualquer uma das ~50 partes do dia. Sem produto,
    basta a primeira parte para achar um exemplo.
    """
    filtros = [("REGIAO", "==", regiao)]
    partes = partes_do_dia(dia)
    if produto:
        filtros.append(("COD_PRODUTO", "==", produto))
    else:
        partes = partes[:1]
    sel = pd.concat([pd.read_parquet(a, filters=filtros) for a in partes],
                    ignore_index=True)
    return sel[sel["PRECO_PROMOCIONAL"].notna()]


def confere_linha(linha: dict, pasta: Path, evidencia: dict) -> bool:
    """Reextrai a linha do JSON BRUTO, com o mesmo parser, e compara.

    Nao basta conferir a linha contra ela mesma. A prova real e reextrair o
    preco do corpo original e chegar exatamente a mesma linha (mesmo hash).
    """
    caminho = arm.caminho_bruto(pasta, evidencia["id_evidencia"])
    if not caminho.exists():
        return False
    bruto = json.loads(arm.le_bruto(caminho))
    registros = [h for h in bruto.get("results", []) if h]
    refeitas, _ = transform.normaliza(
        registros,
        agora=datetime.fromisoformat(str(linha["EXTRAIDO_EM"])),
        id_evidencia=evidencia["id_evidencia"],
    )
    alvo = [l for l in refeitas
            if l["COD_PRODUTO"] == linha["COD_PRODUTO"] and l["REGIAO"] == linha["REGIAO"]]
    gravada = {c: linha[c] for c in transform.COLUNAS}
    return bool(alvo) and hash_linha(alvo[0]) == hash_linha(gravada)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--produto", help="COD_PRODUTO (padrao: o primeiro com preco)")
    p.add_argument("--regiao", default="uberlandia")
    p.add_argument("--data", help="AAAAMMDD da coleta (padrao: a mais recente)")
    p.add_argument("--evidencias", type=Path, default=EVIDENCIAS)
    args = p.parse_args(argv)

    dia = args.data or dia_mais_recente()
    if not dia or not partes_do_dia(dia):
        print(f"nenhuma coleta {dia or ''} em {DATA} - rode a coleta antes", file=sys.stderr)
        return 1

    sel = _linhas(dia, args.regiao, args.produto)
    if "ID_EVIDENCIA" not in sel.columns:
        print(f"a coleta {dia} e anterior a camada de evidencias "
              f"(sem ID_EVIDENCIA). Rode uma coleta nova.", file=sys.stderr)
        return 1
    if sel.empty:
        print("nenhuma linha com preco para esse produto/regiao", file=sys.stderr)
        return 1

    linha = sel.iloc[0].to_dict()
    raiz = args.evidencias
    achada = _evidencia(raiz, linha["ID_EVIDENCIA"])
    if achada is None:
        print(f"evidencia {linha['ID_EVIDENCIA']} nao encontrada em {raiz}",
              file=sys.stderr)
        return 1
    pasta, ev = achada

    integra = confere_linha(linha, pasta, ev)
    print(monta(linha, ev, integra))

    r = verifica_execucao(pasta, raiz, EV_ROBO)
    print()
    print(f"Selo do dia: {r.resumo()}")
    return 0 if integra and r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
