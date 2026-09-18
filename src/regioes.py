"""Mapa das regiões comerciais da Leroy Merlin para Unidades Federativas.

As 28 chaves que aparecem em `regionalAttributes` são regiões COMERCIAIS, não UFs.
São Paulo, por exemplo, é fatiado em 8 regiões com preços independentes.

`outros` é um balde de cobertura nacional sem UF própria e fica de fora da coleta,
conforme decidido: só coletamos regiões associadas a uma UF real.
"""

# regiao (chave do indice) -> sigla da UF
REGIAO_PARA_UF: dict[str, str] = {
    # São Paulo — 8 regiões comerciais distintas
    "grande_sao_paulo": "SP",
    "campinas": "SP",
    "rib_preto": "SP",
    "sorocaba": "SP",
    "vale_do_paraiba": "SP",
    "sao_jose_do_rio_preto": "SP",
    "baixada_santista": "SP",
    "bauru": "SP",
    # Paraná
    "curitiba": "PR",
    "londrina": "PR",
    # Minas Gerais
    "belo_horizonte": "MG",
    "uberlandia": "MG",
    # Santa Catarina
    "joinville": "SC",
    "santa_catarina": "SC",
    # uma região por UF
    "rio_de_janeiro": "RJ",
    "grande_porto_alegre": "RS",
    "brasilia": "DF",
    "goiania": "GO",
    "ceara": "CE",
    "mato_grosso_do_sul": "MS",
    "mato_grosso": "MT",
    "alagoas": "AL",
    "rio_grande_do_norte": "RN",
    "espirito_santo": "ES",
    "acre": "AC",
    "rondonia": "RO",
    "bahia": "BA",
}

# Chaves presentes no índice que NÃO mapeiam para uma UF — descartadas na coleta.
REGIOES_IGNORADAS: frozenset[str] = frozenset({"outros"})

# Nome legível, para relatório
REGIAO_NOME: dict[str, str] = {
    "grande_sao_paulo": "Grande São Paulo",
    "campinas": "Campinas",
    "rib_preto": "Ribeirão Preto",
    "sorocaba": "Sorocaba",
    "vale_do_paraiba": "Vale do Paraíba",
    "sao_jose_do_rio_preto": "São José do Rio Preto",
    "baixada_santista": "Baixada Santista",
    "bauru": "Bauru",
    "curitiba": "Curitiba",
    "londrina": "Londrina",
    "belo_horizonte": "Belo Horizonte",
    "uberlandia": "Uberlândia",
    "joinville": "Joinville",
    "santa_catarina": "Santa Catarina",
    "rio_de_janeiro": "Rio de Janeiro",
    "grande_porto_alegre": "Grande Porto Alegre",
    "brasilia": "Brasília",
    "goiania": "Goiânia",
    "ceara": "Ceará",
    "mato_grosso_do_sul": "Mato Grosso do Sul",
    "mato_grosso": "Mato Grosso",
    "alagoas": "Alagoas",
    "rio_grande_do_norte": "Rio Grande do Norte",
    "espirito_santo": "Espírito Santo",
    "acre": "Acre",
    "rondonia": "Rondônia",
    "bahia": "Bahia",
}

UFS_COBERTAS: frozenset[str] = frozenset(REGIAO_PARA_UF.values())


def uf_de(regiao: str) -> str | None:
    """UF da região, ou None se a região não tem UF (deve ser descartada)."""
    return REGIAO_PARA_UF.get(regiao)


def regioes_desconhecidas(chaves: set[str]) -> set[str]:
    """Regiões vistas no índice que não estão no mapa nem na lista de ignoradas.

    Serve de canário: se a Leroy abrir uma região nova, a coleta avisa em vez
    de descartá-la silenciosamente.
    """
    return chaves - set(REGIAO_PARA_UF) - REGIOES_IGNORADAS
