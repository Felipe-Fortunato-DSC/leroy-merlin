"""Onde a evidência mora em disco.

Layout — uma pasta por execução, autocontida:

    evidencias/
      {robo}/{AAAA-MM-DD}/{HHMMSS}/
          EV-LM-20261008-000001.json.gz     corpo bruto de cada resposta, gzip
          indice.jsonl                      uma linha por evidência
          selo.json                         hash do dia + assinatura
      _chaves/{robo}.ed25519[.pub]          chave de assinatura (ou EVIDENCIA_CHAVES)

Rodar de novo no mesmo dia cria outra pasta de execução; nada é renomeado nem
apagado. A Snowflake guarda só a última carga do dia, e a pasta dela é a que
tem o selo mais recente.
"""
from __future__ import annotations

import gzip
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .hashing import json_canonico

BRASILIA = ZoneInfo("America/Sao_Paulo")
RAIZ_PADRAO = Path(os.getenv("EVIDENCIA_RAIZ", "evidencias"))

INDICE = "indice.jsonl"
SELO = "selo.json"
_RE_BRUTO = re.compile(r"^EV-[A-Z0-9]{1,8}-\d{8}-(\d{6})\.json\.gz$")


# -- pastas ----------------------------------------------------------------
def pasta_dia(raiz: Path, robo: str, dia: date) -> Path:
    return raiz / robo / f"{dia:%Y-%m-%d}"


def nova_execucao(raiz: Path, robo: str, dia: date) -> Path:
    """Cria a pasta de uma execução, nomeada pela hora de início em Brasília."""
    base = pasta_dia(raiz, robo, dia) / f"{datetime.now(BRASILIA):%H%M%S}"
    pasta, n = base, 1
    while pasta.exists():
        n += 1
        pasta = base.with_name(f"{base.name}-{n}")
    pasta.mkdir(parents=True)
    return pasta


def execucoes(raiz: Path, robo: str, dia: date) -> list[Path]:
    """Pastas de execução do dia, em ordem cronológica."""
    p = pasta_dia(raiz, robo, dia)
    return sorted(d for d in p.iterdir() if d.is_dir()) if p.exists() else []


def execucao_selada(raiz: Path, robo: str, dia: date) -> Path | None:
    """A última execução selada do dia — a que corresponde à Snowflake."""
    seladas = [d for d in execucoes(raiz, robo, dia) if (d / SELO).exists()]
    return seladas[-1] if seladas else None


def ultimo_seq(raiz: Path, robo: str, dia: date) -> int:
    """Maior sequência já usada no dia, em qualquer execução.

    Lida dos .gz, não do índice: se uma execução morreu entre gravar o bruto
    e indexá-lo, o número continua ocupado e não é reaproveitado.
    """
    seqs = [int(m.group(1)) for d in execucoes(raiz, robo, dia)
            for f in d.glob("EV-*.json.gz") if (m := _RE_BRUTO.match(f.name))]
    return max(seqs, default=0)


def localiza_bruto(raiz: Path, robo: str, dia: date, id_evidencia: str) -> Path | None:
    for d in reversed(execucoes(raiz, robo, dia)):
        if (f := caminho_bruto(d, id_evidencia)).exists():
            return f
    return None


def caminho_bruto(pasta: Path, id_evidencia: str) -> Path:
    return pasta / f"{id_evidencia}.json.gz"


def caminho_chave(raiz: Path, robo: str, publica: bool = False) -> Path:
    pasta = Path(os.getenv("EVIDENCIA_CHAVES") or raiz / "_chaves")
    return pasta / f"{robo}.ed25519{'.pub' if publica else ''}"


# -- arquivos --------------------------------------------------------------
def grava_bruto(destino: Path, corpo: bytes) -> None:
    """Grava o corpo em gzip, de forma atômica e reprodutível.

    `mtime=0` faz os mesmos bytes produzirem sempre o mesmo .gz. A escrita em
    arquivo temporário + `os.replace` garante que um processo morto no meio não
    deixa um .gz truncado se passando por evidência.
    """
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    # GzipFile não fecha um `fileobj` recebido: o `with` externo é que garante
    # o arquivo fechado (e completo) antes do os.replace.
    with tmp.open("wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as g:
        g.write(corpo)
    os.replace(tmp, destino)


def le_bruto(origem: Path) -> bytes:
    """Devolve os bytes originais, exatamente como foram recebidos."""
    with gzip.open(origem, "rb") as g:
        return g.read()


def anexa_indice(pasta: Path, registro: dict) -> None:
    """Acrescenta uma linha ao índice, com fsync.

    Sem o fsync, uma queda de energia pode perder o registro de uma evidência
    cujo .gz já está no disco.
    """
    with (pasta / INDICE).open("ab") as f:
        f.write(json_canonico(registro) + b"\n")
        f.flush()
        os.fsync(f.fileno())


def le_indice(pasta: Path) -> list[dict]:
    p = pasta / INDICE
    if not p.exists():
        return []
    with p.open("rb") as f:
        return [json.loads(linha) for linha in f if linha.strip()]


def grava_json(destino: Path, obj: dict) -> None:
    tmp = destino.with_suffix(".tmp")
    tmp.write_bytes(json_canonico(obj))
    os.replace(tmp, destino)


def le_json(origem: Path) -> dict | None:
    return json.loads(origem.read_text(encoding="utf-8")) if origem.exists() else None
