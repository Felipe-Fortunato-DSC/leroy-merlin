"""Camada de evidências reutilizável para robôs de coleta.

Garante que cada dado coletado carregue a prova técnica de quando e de onde
veio, resistente a alteração posterior:

    from evidencia import Registrador, hash_linha

    reg = Registrador(robo="leroy_merlin_precos", sigla="LM")
    ev = reg.registrar(corpo=r.content, endpoint=url, dt_requisicao=t0)
    linhas = [... cada uma com "ID_EVIDENCIA": ev.id_evidencia ...]
    ev = reg.fechar(ev, [hash_linha(l) for l in linhas])
    ...
    sela(reg.pasta, reg.raiz, reg.robo)          # no fim da carga

Regra de ouro: a evidência é gravada ANTES do dado derivado dela. Um .gz
órfão é um incômodo; um preço sem evidência é uma auditoria perdida.
"""
from .hashing import digest_linhas, hash_do_dia, hash_linha, sha256_bytes
from .registro import (
    Evidencia, EvidenciaError, Registrador, hoje_brt, id_selo, parse_id,
)
from .selo import SeloError, gera_chave, sela
from .verificacao import Resultado, verifica_execucao

__all__ = [
    "Evidencia", "EvidenciaError", "Registrador", "hoje_brt", "id_selo", "parse_id",
    "digest_linhas", "hash_do_dia", "hash_linha", "sha256_bytes",
    "SeloError", "gera_chave", "sela",
    "Resultado", "verifica_execucao",
]
