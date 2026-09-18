"""Identificação do vendedor.

O que o índice Algolia fornece:
  * `boitataFacets.sellers`  — nomes fantasia dos vendedores (ex: "Tubrax")
  * `marketplaceSellers`     — IDs internos dos vendedores de marketplace

O que ele NÃO fornece: CNPJ ou razão social de vendedor de marketplace.
Também não estão na página do produto — checamos: o HTML de um produto vendido
pela Tubrax traz apenas os CNPJs da própria Leroy, no rodapé. O bloco
"Vendido e entregue por" é renderizado no cliente, atrás do DataDome, e por
isso não é coletável em volume.

Por isso NÃO há coluna de CNPJ na tabela: ela só teria valor para a Leroy, e
esse valor é constante — já derivável de SELLER_TIPO = 'LEROY'. O CNPJ da Leroy
fica registrado aqui como constante, e `src/vendedores.py` gera a planilha de
enriquecimento com uma coluna de CNPJ em branco para preencher.
"""
from __future__ import annotations

SELLER_LEROY = "Leroy Merlin"

# CNPJ e razão social lidos do rodapé de www.leroymerlin.com.br em 14/09/2026.
# O rodapé cita duas inscrições da mesma raiz (01.438.784): a matriz
# 0001-05 e a filial 0048-60. Usamos a matriz.
# Não vira coluna da tabela — ver docstring do módulo.
LEROY_CNPJ = "01.438.784/0001-05"
LEROY_RAZAO_SOCIAL = "Leroy Merlin Cia Brasileira de Bricolagem"

TIPO_LEROY = "LEROY"
TIPO_MARKETPLACE = "MARKETPLACE"


def identifica(sellers: list[str], marketplace_ids: list[str] | None) -> dict:
    """Resolve a identificação do vendedor de um produto.

    Um produto pode ser ofertado por mais de um vendedor, mas o índice traz um
    único preço por região — não um preço por vendedor. Por isso NÃO explodimos
    a linha por vendedor: isso duplicaria o mesmo preço e criaria uma relação
    que o dado de origem não sustenta. Registramos o vendedor principal, a
    lista completa e a contagem.

    Quando a Leroy está entre os vendedores, ela é a principal: o preço
    regional é a estrutura de preço dela.
    """
    sellers = [s for s in (sellers or []) if s]
    ids = [i for i in (marketplace_ids or []) if i]

    if SELLER_LEROY in sellers:
        principal, tipo = SELLER_LEROY, TIPO_LEROY
    elif sellers:
        principal, tipo = sellers[0], TIPO_MARKETPLACE
    else:
        principal, tipo = None, None

    terceiros = [s for s in sellers if s != SELLER_LEROY]

    return {
        "SELLER_PRINCIPAL": principal,
        "SELLER_TIPO": tipo,
        "SELLER_ID": ids[0] if ids else None,
        "SELLERS_TODOS": "|".join(sellers) or None,
        "QTD_SELLERS": len(sellers),
        "TEM_MARKETPLACE": len(terceiros) > 0,
    }
