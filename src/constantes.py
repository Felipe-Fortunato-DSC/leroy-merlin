"""Caminhos e identidade do coletor, compartilhados pelos módulos de `src`."""
from __future__ import annotations

import os
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"

# Raiz das evidências. A mesma variável que o pacote `evidencia` lê, mas aqui
# ancorada na raiz do projeto: assim a coleta grava no mesmo lugar qualquer
# que seja o diretório de onde é chamada.
EVIDENCIAS = Path(os.getenv("EVIDENCIA_RAIZ", RAIZ / "evidencias"))

# Identidade deste robô nas evidências: ROBO nomeia a pasta e a chave de
# assinatura, SIGLA compõe o ID_EVIDENCIA (EV-LM-20261008-000001).
EV_ROBO = "leroy_merlin_precos"
EV_SIGLA = "LM"

# leroy_AAAAMMDD_pNNN.parquet — um arquivo por bloco da coleta completa.
# Amostras (--amostra) gravam em leroy_amostra_*, para nunca sobrescrever as
# partes da coleta completa do mesmo dia.
_RE_PARTE = re.compile(r"^leroy_(\d{8})_p(\d{3})\.parquet$")


def nome_parte(dia: str, n: int, amostra: bool = False) -> str:
    """Nome do Parquet do bloco `n` da coleta de `dia` (AAAAMMDD)."""
    prefixo = "leroy_amostra" if amostra else "leroy"
    return f"{prefixo}_{dia}_p{n:03d}.parquet"


def partes_do_dia(dia: str, pasta: Path = DATA) -> list[Path]:
    """Partes da coleta completa de `dia` (AAAAMMDD), em ordem."""
    return sorted(p for p in pasta.glob(f"leroy_{dia}_p*.parquet")
                  if _RE_PARTE.match(p.name))


def dia_mais_recente(pasta: Path = DATA) -> str | None:
    """Data (AAAAMMDD) da coleta completa mais recente em `pasta`."""
    dias = [m.group(1) for p in pasta.glob("leroy_*.parquet")
            if (m := _RE_PARTE.match(p.name))]
    return max(dias, default=None)
