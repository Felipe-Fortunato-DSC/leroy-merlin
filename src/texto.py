"""Limpeza de texto vindo da fonte.

Módulo próprio porque é usado por `transform` e por `sellers`, e mantê-lo em
`transform` obrigava `sellers` a um import circular.
"""
from __future__ import annotations

# Caracteres de controle C0 (0x00-0x1F) e C1 (0x7F-0x9F). Aparecem no índice
# da Leroy como lixo no fim de algumas descrições ("... Kit Gamer Skill\x7f").
#
# Precisam sair por dois motivos. São invisíveis e não significam nada num nome
# de produto, e quebram a conferência do hash em SQL: o json.dumps do Python
# deixa o 0x7F cru, enquanto o TO_JSON da Snowflake o escapa como \u007f, e os
# dois hashes deixam de bater numa linha que ninguém adulterou.
#
# ATENÇÃO: qualquer mudança aqui muda o hash das linhas das coletas futuras.
_CONTROLES = {c: None for c in [*range(0x00, 0x20), *range(0x7F, 0xA0)]}


def limpa(v) -> str | None:
    """Texto da fonte sem caracteres de controle; vazio vira None."""
    if v is None:
        return None
    return str(v).translate(_CONTROLES).strip() or None
