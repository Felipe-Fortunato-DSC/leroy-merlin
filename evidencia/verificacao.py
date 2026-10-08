"""Verificação de uma execução em disco: brutos, hash do dia e assinatura.

  1. cada .gz reaberto tem o SHA-256 registrado no índice  -> bruto intacto
  2. o hash do dia recalculado do índice é o do selo          -> índice intacto
  3. a assinatura do selo confere com a chave pública         -> selo legítimo

O lado da Snowflake (preços x evidências) é conferido em SQL — ver
snowflake.sql_auditoria.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import armazenamento as arm
from .hashing import hash_do_dia, sha256_bytes
from .selo import SeloError, assinatura_confere, carrega_publica


@dataclass
class Resultado:
    pasta: Path
    n_evidencias: int
    problemas: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problemas

    def resumo(self) -> str:
        if self.ok:
            return f"{self.pasta}: ÍNTEGRO ({self.n_evidencias} evidências, selo confere)"
        return (f"{self.pasta}: FALHA — {len(self.problemas)} problema(s): "
                + "; ".join(self.problemas[:5]))


def verifica_execucao(pasta: Path, raiz: Path, robo: str) -> Resultado:
    registros = arm.le_indice(pasta)
    r = Resultado(pasta, len(registros))

    for reg in registros:
        caminho = arm.caminho_bruto(pasta, reg["id_evidencia"])
        if not caminho.exists():
            r.problemas.append(f"{reg['id_evidencia']}: bruto ausente")
        elif sha256_bytes(arm.le_bruto(caminho)) != reg["hash_bruto"]:
            r.problemas.append(f"{reg['id_evidencia']}: bruto alterado")

    selo = arm.le_json(pasta / arm.SELO)
    if selo is None:
        r.problemas.append("sem selo")
        return r
    if hash_do_dia(registros) != selo["hash_dia"]:
        r.problemas.append("índice alterado: o hash do dia não é o do selo")
    try:
        if not assinatura_confere(selo, carrega_publica(raiz, robo)):
            r.problemas.append("assinatura do selo inválida")
    except SeloError as exc:
        r.problemas.append(str(exc))
    return r
