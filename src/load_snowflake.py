"""Carga na Snowflake — BASES_SPDO.DB_SANDBOX.TBL_LEROY_TESTE.

Conexão em duas modalidades, nesta ordem:

  1. Variáveis de ambiente (modo container/AWS). Chave RSA vinda de
     SNOWFLAKE_PRIVATE_KEY (PEM inteiro) ou SNOWFLAKE_PRIVATE_KEY_FILE.
  2. O `config.py` do MCP em D:\\Dev_Projects\\MCP\\snowflake (modo local),
     reaproveitando a credencial que já existe na máquina em vez de duplicá-la.

A carga é idempotente por dia de coleta — a data de Brasília em que a
execução começou, a mesma das evidências. Cada execução apaga os preços e as
evidências desse dia antes de inserir: rodar duas vezes não duplica, e a base
fica sempre com a última carga do dia.

    python -m src.load_snowflake      # imprime a DDL completa
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from evidencia import snowflake as sf_evid

from .transform import COLUNAS, TIPOS_HASH

log = logging.getLogger(__name__)

DATABASE = os.getenv("SNOWFLAKE_DATABASE", "BASES_SPDO")
SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "DB_SANDBOX")
TABELA = os.getenv("SNOWFLAKE_TABLE", "TBL_LEROY_TESTE")

MCP_SNOWFLAKE_DIR = Path(os.getenv("MCP_SNOWFLAKE_DIR", r"D:\Dev_Projects\MCP\snowflake"))

DDL = f"""
CREATE TABLE IF NOT EXISTS {DATABASE}.{SCHEMA}.{TABELA} (
    COD_PRODUTO         VARCHAR(32)    NOT NULL,
    EAN_PRINCIPAL       VARCHAR(20),
    EANS_TODOS          VARCHAR(512),
    DESCRICAO           VARCHAR(512),
    DESCRICAO_CURTA     VARCHAR(256),
    CATEGORIA_N1        VARCHAR(256),
    CATEGORIA_N2        VARCHAR(512),
    UNIDADE             VARCHAR(32),
    SELLER_PRINCIPAL    VARCHAR(256),
    SELLER_TIPO         VARCHAR(16),
    SELLER_ID           VARCHAR(64),
    SELLERS_TODOS       VARCHAR(2048),
    QTD_SELLERS         NUMBER(5,0),
    TEM_MARKETPLACE     BOOLEAN,
    REGIAO              VARCHAR(64)    NOT NULL,
    REGIAO_NOME         VARCHAR(64),
    UF                  CHAR(2)        NOT NULL,
    PRECO_PROMOCIONAL   NUMBER(12,2),
    PRECO_ORIGINAL      NUMBER(12,2),
    DISPONIVEL          BOOLEAN,
    TEM_ESTOQUE         BOOLEAN,
    URL                 VARCHAR(1024),
    EXTRAIDO_EM         TIMESTAMP_NTZ  NOT NULL,
    ID_EVIDENCIA        VARCHAR(32)
)
COMMENT = 'Precos Leroy Merlin por regiao comercial, incluindo marketplace. Grao: SKU x REGIAO (o indice traz um preco por regiao, nao por vendedor). Sem coluna de CNPJ: o CNPJ de vendedor de marketplace nao e exposto publicamente, e o da Leroy e constante (ver SELLER_TIPO). EXTRAIDO_EM e o inicio da execucao, em UTC. Fonte: indice Algolia publico production_products. Evidencias em TBL_EVIDENCIAS_TESTE.'
"""

# Dia de coleta (AAAAMMDD) de uma linha de preço: a data de Brasília do início
# da execução. EXTRAIDO_EM é gravado em UTC; uma coleta às 22h de Brasília já
# é dia seguinte em UTC, e sem a conversão cairia na carga do dia errado.
EXPR_DIA = "TO_CHAR(CONVERT_TIMEZONE('UTC', 'America/Sao_Paulo', {p}EXTRAIDO_EM), 'YYYYMMDD')"


class CargaError(RuntimeError):
    pass


def _conexao_por_ambiente():
    """Conexão a partir de variáveis de ambiente (container)."""
    user = os.getenv("SNOWFLAKE_USER")
    account = os.getenv("SNOWFLAKE_ACCOUNT")
    if not (user and account):
        return None

    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    import snowflake.connector

    pem = os.getenv("SNOWFLAKE_PRIVATE_KEY")
    if not pem:
        caminho = os.getenv("SNOWFLAKE_PRIVATE_KEY_FILE")
        if not caminho:
            raise CargaError(
                "SNOWFLAKE_USER/ACCOUNT definidos, mas nem SNOWFLAKE_PRIVATE_KEY "
                "nem SNOWFLAKE_PRIVATE_KEY_FILE foram informados."
            )
        pem = Path(caminho).read_text()

    senha = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    chave = serialization.load_pem_private_key(
        pem.encode(),
        password=senha.encode() if senha else None,
        backend=default_backend(),
    ).private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    log.info("conectando via variáveis de ambiente (user=%s)", user)
    return snowflake.connector.connect(
        user=user,
        account=account,
        private_key=chave,
        role=os.getenv("SNOWFLAKE_ROLE"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=DATABASE,
        schema=SCHEMA,
        application="leroy-price-collector",
    )


def _conexao_por_mcp():
    """Conexão reaproveitando o config.py do MCP Snowflake local."""
    if not (MCP_SNOWFLAKE_DIR / "config.py").exists():
        raise CargaError(
            f"Sem credenciais: nem variáveis de ambiente nem config.py em {MCP_SNOWFLAKE_DIR}.\n"
            "Defina SNOWFLAKE_USER / SNOWFLAKE_ACCOUNT / SNOWFLAKE_PRIVATE_KEY(_FILE), "
            "ou ajuste MCP_SNOWFLAKE_DIR."
        )
    sys.path.insert(0, str(MCP_SNOWFLAKE_DIR))
    import config  # type: ignore

    log.info("conectando via config.py do MCP em %s", MCP_SNOWFLAKE_DIR)
    return config.get_connection()


def conecta():
    conn = _conexao_por_ambiente()
    if conn is None:
        conn = _conexao_por_mcp()
    with conn.cursor() as cur:
        cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE()")
        usuario, role, wh = cur.fetchone()
    log.info("conectado — user=%s role=%s warehouse=%s", usuario, role, wh)
    if not wh:
        raise CargaError(
            "Nenhum warehouse ativo na sessão. Defina SNOWFLAKE_WAREHOUSE "
            "(ou DEFAULT_WRH no arquivo de credenciais)."
        )
    return conn


def _prepara_df(df: pd.DataFrame) -> pd.DataFrame:
    # Só as colunas da tabela: Parquets antigos ainda trazem HASH_LINHA.
    df = df[COLUNAS].copy()
    # write_pandas escreve datetime64[ns] em Parquet como inteiro de nanossegundos,
    # e o COPY INTO o lê como segundos desde a época — o resultado é um timestamp
    # absurdo que nem dá para consultar. Enviar texto ISO e deixar a Snowflake
    # fazer o cast para TIMESTAMP_NTZ evita a ambiguidade de unidade.
    df["EXTRAIDO_EM"] = pd.to_datetime(df["EXTRAIDO_EM"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    for c in ("PRECO_PROMOCIONAL", "PRECO_ORIGINAL"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def sql_auditoria(dia: date | None = None) -> str:
    return sf_evid.sql_auditoria(DATABASE, SCHEMA, TABELA, COLUNAS, TIPOS_HASH,
                                 EXPR_DIA.format(p="p."), dia)


def ddl_completo() -> list[str]:
    """Todos os objetos, na ordem de criação."""
    return [
        DDL,
        f"ALTER TABLE {DATABASE}.{SCHEMA}.{TABELA} ADD COLUMN IF NOT EXISTS ID_EVIDENCIA VARCHAR(32)",
        sf_evid.ddl_tabela(DATABASE, SCHEMA),
        sf_evid.ddl_view(DATABASE, SCHEMA, TABELA, COLUNAS, TIPOS_HASH, EXPR_DIA.format(p="p.")),
    ]


def prepara_tabela(conn, dia: date, sigla: str, recriar: bool = False,
                   apagar_evidencias: bool = True) -> None:
    """DDL e limpeza da carga do dia. UMA vez por execução.

    `apagar_evidencias=False` serve à recarga, que repõe só os preços.
    """
    with conn.cursor() as cur:
        cur.execute(f"USE DATABASE {DATABASE}")
        cur.execute(f"USE SCHEMA {SCHEMA}")
        if recriar:
            log.warning("DROP TABLE %s.%s.%s (--recriar)", DATABASE, SCHEMA, TABELA)
            cur.execute(f"DROP TABLE IF EXISTS {DATABASE}.{SCHEMA}.{TABELA}")
        for cmd in ddl_completo():
            cur.execute(cmd)
        cur.execute(f"DELETE FROM {DATABASE}.{SCHEMA}.{TABELA} "
                    f"WHERE {EXPR_DIA.format(p='')} = %s", (f"{dia:%Y%m%d}",))
        removidas = cur.rowcount or 0
    conn.commit()
    if removidas:
        log.info("removidas %d linhas de preço da carga anterior de %s", removidas, dia)
    if apagar_evidencias:
        n = sf_evid.apaga_dia(conn, DATABASE, SCHEMA, sigla, dia)
        if n:
            log.info("removidas %d evidências da carga anterior de %s", n, dia)


def insere(conn, df: pd.DataFrame) -> int:
    """Insere um bloco na tabela já preparada."""
    from snowflake.connector.pandas_tools import write_pandas

    ok, _, n_linhas, _ = write_pandas(
        conn, _prepara_df(df), table_name=TABELA, database=DATABASE, schema=SCHEMA,
        quote_identifiers=False, chunk_size=250_000, auto_create_table=False,
    )
    if not ok:
        raise CargaError("write_pandas retornou falha")
    conn.commit()
    return n_linhas



if __name__ == "__main__":
    for comando in ddl_completo():
        print(comando.strip() + ";\n")
