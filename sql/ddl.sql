CREATE TABLE IF NOT EXISTS BASES_SPDO.DB_SANDBOX.TBL_LEROY_TESTE (
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
COMMENT = 'Precos Leroy Merlin por regiao comercial, incluindo marketplace. Grao: SKU x REGIAO (o indice traz um preco por regiao, nao por vendedor). Sem coluna de CNPJ: o CNPJ de vendedor de marketplace nao e exposto publicamente, e o da Leroy e constante (ver SELLER_TIPO). EXTRAIDO_EM e o inicio da execucao, em UTC. Fonte: indice Algolia publico production_products. Evidencias em TBL_EVIDENCIAS_TESTE.';

ALTER TABLE BASES_SPDO.DB_SANDBOX.TBL_LEROY_TESTE ADD COLUMN IF NOT EXISTS ID_EVIDENCIA VARCHAR(32);

CREATE TABLE IF NOT EXISTS BASES_SPDO.DB_SANDBOX.TBL_EVIDENCIAS_TESTE (
    ID_EVIDENCIA   VARCHAR(32)      NOT NULL COMMENT 'EV-{sigla do robo}-{AAAAMMDD}-{seq}, ou -SELO na linha de fechamento do dia',
    DT_REQUISICAO  TIMESTAMP_TZ(3)  NOT NULL COMMENT 'Instante real da requisicao, medido antes do envio. No selo: quando foi assinado',
    ENDPOINT       VARCHAR(1024)    NOT NULL COMMENT 'URL requisitada. No selo: selo:ed25519:{impressao da chave}',
    HASH_BRUTO     CHAR(64)         NOT NULL COMMENT 'SHA-256 dos bytes exatos recebidos (corpo em disco, gzip). No selo: o hash do dia',
    DIGEST_LINHAS  CHAR(64)                  COMMENT 'Resumo das linhas de preco derivadas desta resposta. Nulo no selo',
    ASSINATURA     VARCHAR(88)               COMMENT 'Ed25519 sobre ID_EVIDENCIA|hash do dia. Preenchida so no selo',
    CONSTRAINT PK_TBL_EVIDENCIAS_TESTE PRIMARY KEY (ID_EVIDENCIA)
)
COMMENT = 'Evidencia tecnica de cada resposta HTTP que originou precos, e o selo assinado de cada dia. Validacao em VW_AUDITORIA_EVIDENCIAS.';

CREATE OR REPLACE VIEW BASES_SPDO.DB_SANDBOX.VW_AUDITORIA_EVIDENCIAS AS
WITH ev AS (
    SELECT e.*, SPLIT_PART(e.ID_EVIDENCIA, '-', 2) AS SIGLA,
           SPLIT_PART(e.ID_EVIDENCIA, '-', 3) AS DIA
      FROM BASES_SPDO.DB_SANDBOX.TBL_EVIDENCIAS_TESTE e
     
),
linhas AS (
    SELECT ID_EVIDENCIA, COUNT(*) AS N,
           SHA2(LISTAGG(H, '') WITHIN GROUP (ORDER BY H), 256) AS DIGEST
      FROM (SELECT p.ID_EVIDENCIA, SHA2(TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(
               'CATEGORIA_N1', p.CATEGORIA_N1,
               'CATEGORIA_N2', p.CATEGORIA_N2,
               'COD_PRODUTO', p.COD_PRODUTO,
               'DESCRICAO', p.DESCRICAO,
               'DESCRICAO_CURTA', p.DESCRICAO_CURTA,
               'DISPONIVEL', p.DISPONIVEL,
               'EANS_TODOS', p.EANS_TODOS,
               'EAN_PRINCIPAL', p.EAN_PRINCIPAL,
               'EXTRAIDO_EM', TO_CHAR(p.EXTRAIDO_EM, 'YYYY-MM-DD"T"HH24:MI:SS'),
               'ID_EVIDENCIA', p.ID_EVIDENCIA,
               'PRECO_ORIGINAL', TO_CHAR(p.PRECO_ORIGINAL),
               'PRECO_PROMOCIONAL', TO_CHAR(p.PRECO_PROMOCIONAL),
               'QTD_SELLERS', p.QTD_SELLERS,
               'REGIAO', p.REGIAO,
               'REGIAO_NOME', p.REGIAO_NOME,
               'SELLERS_TODOS', p.SELLERS_TODOS,
               'SELLER_ID', p.SELLER_ID,
               'SELLER_PRINCIPAL', p.SELLER_PRINCIPAL,
               'SELLER_TIPO', p.SELLER_TIPO,
               'TEM_ESTOQUE', p.TEM_ESTOQUE,
               'TEM_MARKETPLACE', p.TEM_MARKETPLACE,
               'UF', p.UF,
               'UNIDADE', p.UNIDADE,
               'URL', p.URL)), 256) AS H
              FROM BASES_SPDO.DB_SANDBOX.TBL_LEROY_TESTE p JOIN ev ON ev.ID_EVIDENCIA = p.ID_EVIDENCIA)
     GROUP BY ID_EVIDENCIA
),
orfas AS (
    SELECT TO_CHAR(CONVERT_TIMEZONE('UTC', 'America/Sao_Paulo', p.EXTRAIDO_EM), 'YYYYMMDD') AS DIA, p.ID_EVIDENCIA, COUNT(*) AS N
      FROM BASES_SPDO.DB_SANDBOX.TBL_LEROY_TESTE p
     WHERE NOT EXISTS (SELECT 1 FROM ev WHERE ev.ID_EVIDENCIA = p.ID_EVIDENCIA)
       
     GROUP BY 1, 2
),
dia AS (
    SELECT e.SIGLA, e.DIA, SHA2(LISTAGG(e.ID_EVIDENCIA || '|' || TO_CHAR(CONVERT_TIMEZONE('UTC', e.DT_REQUISICAO), 'YYYY-MM-DD"T"HH24:MI:SS.FF3"Z"') || '|' || SHA2(e.ENDPOINT, 256) || '|' || e.HASH_BRUTO || '|' || e.DIGEST_LINHAS, '\n') WITHIN GROUP (ORDER BY e.ID_EVIDENCIA), 256) AS HASH_DIA
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
SELECT DIA, ID_EVIDENCIA, NULL, N, 'linhas_sem_evidencia' FROM orfas;

