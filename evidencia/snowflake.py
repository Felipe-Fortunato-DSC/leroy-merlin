"""Evidências na Snowflake: uma tabela, uma view de auditoria.

    TBL_EVIDENCIAS_TESTE        uma linha por resposta HTTP + uma linha de selo por dia
    VW_AUDITORIA_EVIDENCIAS     status de cada evidência, recalculado em SQL

A tabela de preços só ganha ID_EVIDENCIA. Nada de hash por linha: a view
recalcula o hash de cada linha a partir das próprias colunas, agrega por
evidência e compara com DIGEST_LINHAS. Depois recalcula o hash do dia e
compara com o da linha de selo, cuja assinatura o Python confere.

Cadeia de confiança:

    linha de preço --SQL--> DIGEST_LINHAS --SQL--> hash do dia --Ed25519--> assinatura
    .gz em disco   --SHA-256--> HASH_BRUTO ----------^

O pacote não abre conexão por conta própria: quem chama passa a sua.
"""
from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Iterable

from .armazenamento import BRASILIA
from .registro import id_selo

TBL_EVIDENCIAS = "TBL_EVIDENCIAS_TESTE"
VW_AUDITORIA = "VW_AUDITORIA_EVIDENCIAS"

# Formato canônico do instante, idêntico a registro.instante_canonico.
_DT_CANONICO = """TO_CHAR(CONVERT_TIMEZONE('UTC', {c}), 'YYYY-MM-DD"T"HH24:MI:SS.FF3"Z"')"""


def ddl_tabela(database: str, schema: str, tabela: str = TBL_EVIDENCIAS) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {database}.{schema}.{tabela} (
    ID_EVIDENCIA   VARCHAR(32)      NOT NULL COMMENT 'EV-{{sigla do robo}}-{{AAAAMMDD}}-{{seq}}, ou -SELO na linha de fechamento do dia',
    DT_REQUISICAO  TIMESTAMP_TZ(3)  NOT NULL COMMENT 'Instante real da requisicao, medido antes do envio. No selo: quando foi assinado',
    ENDPOINT       VARCHAR(1024)    NOT NULL COMMENT 'URL requisitada. No selo: selo:ed25519:{{impressao da chave}}',
    HASH_BRUTO     CHAR(64)         NOT NULL COMMENT 'SHA-256 dos bytes exatos recebidos (corpo em disco, gzip). No selo: o hash do dia',
    DIGEST_LINHAS  CHAR(64)                  COMMENT 'Resumo das linhas de preco derivadas desta resposta. Nulo no selo',
    ASSINATURA     VARCHAR(88)               COMMENT 'Ed25519 sobre ID_EVIDENCIA|hash do dia. Preenchida so no selo',
    CONSTRAINT PK_{tabela} PRIMARY KEY (ID_EVIDENCIA)
)
COMMENT = 'Evidencia tecnica de cada resposta HTTP que originou precos, e o selo assinado de cada dia. Validacao em {VW_AUDITORIA}.'
"""


def sql_hash_linha(colunas: Iterable[str], tipos: dict[str, str], alias: str = "p") -> str:
    """Expressão SQL igual a hashing.hash_linha.

    `tipos` marca as colunas que precisam de forma canônica: "dinheiro"
    (NUMBER(12,2) -> '559.84') e "momento" (TIMESTAMP -> '2026-10-08T19:01:38').
    """
    def valor(c: str) -> str:
        tipo = tipos.get(c)
        if tipo == "dinheiro":
            return f"TO_CHAR({alias}.{c})"
        if tipo == "momento":
            return f"""TO_CHAR({alias}.{c}, 'YYYY-MM-DD"T"HH24:MI:SS')"""
        return f"{alias}.{c}"

    pares = ",\n               ".join(f"'{c}', {valor(c)}" for c in sorted(colunas))
    return (f"SHA2(TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(\n               {pares})), 256)")


def _sql_hash_dia(alias: str = "e") -> str:
    """Expressão de agregação igual a hashing.hash_do_dia."""
    dt = _DT_CANONICO.format(c=f"{alias}.DT_REQUISICAO")
    return (f"SHA2(LISTAGG({alias}.ID_EVIDENCIA || '|' || {dt} || '|' || "
            f"SHA2({alias}.ENDPOINT, 256) || '|' || {alias}.HASH_BRUTO || '|' || "
            f"{alias}.DIGEST_LINHAS, '\\n') WITHIN GROUP (ORDER BY {alias}.ID_EVIDENCIA), 256)")


def sql_auditoria(database: str, schema: str, tabela_precos: str,
                  colunas: Iterable[str], tipos: dict[str, str], expr_dia_precos: str,
                  dia: date | None = None, tabela: str = TBL_EVIDENCIAS) -> str:
    """Status de cada evidência (e das linhas de preço sem evidência).

    STATUS:
      integra               linhas conferem e o dia está selado com o hash certo
      linhas_alteradas      UPDATE, INSERT ou DELETE nas linhas desta evidência
      evidencias_alteradas  a tabela de evidências não bate com o hash selado
      dia_nao_selado        sem linha de selo (carga em andamento ou que falhou)
      linhas_sem_evidencia  preços cujo ID_EVIDENCIA não existe na tabela

    A assinatura não é conferida aqui (a Snowflake não verifica Ed25519): ela
    é conferida por `python -m src.auditoria`, sobre o mesmo hash.

    `expr_dia_precos`: expressão AAAAMMDD de uma linha de preço (alias p).
    `dia`: restringe a um dia — a view usa sem filtro, a carga usa com.
    """
    t_ev = f"{database}.{schema}.{tabela}"
    t_p = f"{database}.{schema}.{tabela_precos}"
    filtro_ev = filtro_p = ""
    if dia is not None:
        filtro_ev = f"WHERE SPLIT_PART(e.ID_EVIDENCIA, '-', 3) = '{dia:%Y%m%d}'"
        filtro_p = f"AND {expr_dia_precos} = '{dia:%Y%m%d}'"
    return f"""
WITH ev AS (
    SELECT e.*, SPLIT_PART(e.ID_EVIDENCIA, '-', 2) AS SIGLA,
           SPLIT_PART(e.ID_EVIDENCIA, '-', 3) AS DIA
      FROM {t_ev} e
     {filtro_ev}
),
linhas AS (
    SELECT ID_EVIDENCIA, COUNT(*) AS N,
           SHA2(LISTAGG(H, '') WITHIN GROUP (ORDER BY H), 256) AS DIGEST
      FROM (SELECT p.ID_EVIDENCIA, {sql_hash_linha(colunas, tipos)} AS H
              FROM {t_p} p JOIN ev ON ev.ID_EVIDENCIA = p.ID_EVIDENCIA)
     GROUP BY ID_EVIDENCIA
),
orfas AS (
    SELECT {expr_dia_precos} AS DIA, p.ID_EVIDENCIA, COUNT(*) AS N
      FROM {t_p} p
     WHERE NOT EXISTS (SELECT 1 FROM ev WHERE ev.ID_EVIDENCIA = p.ID_EVIDENCIA)
       {filtro_p}
     GROUP BY 1, 2
),
dia AS (
    SELECT e.SIGLA, e.DIA, {_sql_hash_dia()} AS HASH_DIA
      FROM ev e WHERE e.ASSINATURA IS NULL
     GROUP BY e.SIGLA, e.DIA
),
selo AS (
    SELECT SIGLA, DIA, HASH_BRUTO AS HASH_SELADO FROM ev WHERE ASSINATURA IS NOT NULL
)
SELECT e.DIA, e.ID_EVIDENCIA, e.DT_REQUISICAO, COALESCE(l.N, 0) AS N_LINHAS,
       CASE
           WHEN COALESCE(l.DIGEST, SHA2('', 256)) <> e.DIGEST_LINHAS THEN 'linhas_alteradas'
           WHEN s.HASH_SELADO IS NULL                               THEN 'dia_nao_selado'
           WHEN s.HASH_SELADO <> d.HASH_DIA                         THEN 'evidencias_alteradas'
           ELSE 'integra'
       END AS STATUS
  FROM ev e
  LEFT JOIN linhas l ON l.ID_EVIDENCIA = e.ID_EVIDENCIA
  LEFT JOIN dia d    ON d.SIGLA = e.SIGLA AND d.DIA = e.DIA
  LEFT JOIN selo s   ON s.SIGLA = e.SIGLA AND s.DIA = e.DIA
 WHERE e.ASSINATURA IS NULL
UNION ALL
SELECT DIA, ID_EVIDENCIA, NULL, N, 'linhas_sem_evidencia' FROM orfas
"""


def ddl_view(database: str, schema: str, tabela_precos: str, colunas: Iterable[str],
             tipos: dict[str, str], expr_dia_precos: str) -> str:
    return (f"CREATE OR REPLACE VIEW {database}.{schema}.{VW_AUDITORIA} AS"
            + sql_auditoria(database, schema, tabela_precos, colunas, tipos, expr_dia_precos))


# -- carga -----------------------------------------------------------------
def _brt(instante: str) -> str:
    """Instante canônico (UTC) em ISO com o fuso de Brasília, para TIMESTAMP_TZ."""
    return datetime.fromisoformat(instante).astimezone(BRASILIA).isoformat(timespec="milliseconds")


def prepara(conn, database: str, schema: str, tabela: str = TBL_EVIDENCIAS) -> None:
    with conn.cursor() as cur:
        cur.execute(ddl_tabela(database, schema, tabela))
    conn.commit()


def apaga_dia(conn, database: str, schema: str, sigla: str, dia: date,
              tabela: str = TBL_EVIDENCIAS) -> int:
    """Remove as evidências e o selo de um dia — par do DELETE dos preços."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {database}.{schema}.{tabela} WHERE ID_EVIDENCIA LIKE %s",
                    (f"EV-{sigla}-{dia:%Y%m%d}-%",))
        n = cur.rowcount or 0
    conn.commit()
    return n


def carrega_evidencias(conn, evidencias, database: str, schema: str,
                       tabela: str = TBL_EVIDENCIAS) -> int:
    """Insere evidências (Evidencia ou dict do índice). Idempotente por ID."""
    regs = [asdict(e) if is_dataclass(e) else e for e in evidencias]
    if not regs:
        return 0
    linhas = [(r["id_evidencia"], _brt(r["dt_requisicao"]), r["endpoint"],
               r["hash_bruto"], r["digest_linhas"]) for r in regs]
    alvo = f"{database}.{schema}.{tabela}"
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {alvo} WHERE ID_EVIDENCIA IN ({','.join(['%s'] * len(linhas))})",
                    [l[0] for l in linhas])
        cur.executemany(f"INSERT INTO {alvo} (ID_EVIDENCIA, DT_REQUISICAO, ENDPOINT, "
                        f"HASH_BRUTO, DIGEST_LINHAS) VALUES (%s, %s, %s, %s, %s)", linhas)
    conn.commit()
    return len(linhas)


def carrega_selo(conn, selo: dict, database: str, schema: str,
                 tabela: str = TBL_EVIDENCIAS) -> None:
    alvo = f"{database}.{schema}.{tabela}"
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {alvo} WHERE ID_EVIDENCIA = %s", (selo["id_evidencia"],))
        cur.execute(f"INSERT INTO {alvo} (ID_EVIDENCIA, DT_REQUISICAO, ENDPOINT, "
                    f"HASH_BRUTO, ASSINATURA) VALUES (%s, %s, %s, %s, %s)",
                    (selo["id_evidencia"], _brt(selo["dt_selo"]),
                     f"selo:ed25519:{selo['chave']}", selo["hash_dia"], selo["assinatura"]))
    conn.commit()


def hash_dia_na_base(conn, database: str, schema: str, sigla: str, dia: date,
                     tabela: str = TBL_EVIDENCIAS) -> str | None:
    """O hash do dia recalculado pela Snowflake sobre o que está gravado."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_sql_hash_dia()} FROM {database}.{schema}.{tabela} e "
                    f"WHERE e.ID_EVIDENCIA LIKE %s AND e.ASSINATURA IS NULL",
                    (f"EV-{sigla}-{dia:%Y%m%d}-%",))
        return cur.fetchone()[0]


def selo_na_base(conn, database: str, schema: str, sigla: str, dia: date,
                 tabela: str = TBL_EVIDENCIAS) -> dict | None:
    """A linha de selo do dia, no formato de selo.json (para conferir a assinatura)."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT ID_EVIDENCIA, HASH_BRUTO, ASSINATURA, ENDPOINT "
                    f"FROM {database}.{schema}.{tabela} WHERE ID_EVIDENCIA = %s",
                    (id_selo(sigla, dia),))
        r = cur.fetchone()
    if r is None:
        return None
    return {"id_evidencia": r[0], "hash_dia": r[1], "assinatura": r[2],
            "chave": re.sub(r"^selo:ed25519:", "", r[3])}


def audita(conn, sql: str) -> list[dict]:
    """Executa sql_auditoria e devolve as linhas como dicts."""
    with conn.cursor() as cur:
        cur.execute(sql)
        nomes = [d[0] for d in cur.description]
        return [dict(zip(nomes, r)) for r in cur.fetchall()]
