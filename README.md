# Coleta de preços Leroy Merlin por região

Extrai o catálogo da Leroy Merlin (código, EAN, descrição e preço) — produtos
próprios e de marketplace — com o preço de cada região comercial, e carrega em
`BASES_SPDO.DB_SANDBOX.TBL_LEROY_TESTE`.

## O CAPTCHA do DataDome não afeta esta coleta

O site `www.leroymerlin.com.br` é protegido por DataDome e bloqueia requisições
automatizadas. **Este pipeline nunca requisita esse domínio.** Ele usa duas
fontes públicas que estão fora da proteção:

| Fonte | Para quê | Protegido? |
|---|---|---|
| `cdn.leroymerlin.com.br/sitemaps/produtos_N.xml` | lista canônica de IDs de produto | não |
| `1CF3ZT43ZU-dsn.algolia.net` | registros com EAN, descrição e preços regionais | não (serviço Algolia) |

O índice Algolia é o mesmo que alimenta a busca do site. Sua chave é de
**busca apenas**, publicada de propósito nos bundles JavaScript (é assim que
busca no navegador funciona) e não expira.

A única exceção é `src/remine.py`, que faz **uma** requisição ao site — e só
quando a chave pública é rotacionada, o que é raro. Mesmo esse caso tem
alternativa manual documentada abaixo.

## Instalação

```bash
pip install -r requirements.txt
```

Recomendado usar um ambiente virtual dedicado — `curl_cffi` (opcional, só para
`remine`) exige `cffi>=2.0`, que conflita com o pin do
`snowflake-connector-python`.

## Uso

```bash
# teste representativo (amostra ALEATÓRIA — a ordem do sitemap é enviesada)
python -m src.main --amostra 30000

# catálogo completo
python -m src.main

# só gera o Parquet, sem tocar na Snowflake
python -m src.main --parquet-only

# recria a tabela do zero
python -m src.main --recriar
```

Toda execução grava `data/leroy_AAAAMMDD_pNNN.parquet` (um arquivo por bloco de
2 milhões de linhas — backup e insumo para recarga sem recoletar) e
`data/ultima_execucao.json` com as estatísticas.

A coleta, a gravação e a carga acontecem **em blocos**: o catálogo completo
passa de 20 milhões de linhas e não caberia num único DataFrame.

A carga é **idempotente por dia**: antes de inserir, remove as linhas de
`EXTRAIDO_EM` da mesma data. Rodar duas vezes no mesmo dia não duplica.

## Credenciais

**Local** — reaproveita automaticamente o `config.py` do MCP Snowflake em
`D:\Dev_Projects\MCP\snowflake`. Nada a configurar.

**Container / AWS** — via variáveis de ambiente, que têm precedência:

```
SNOWFLAKE_USER                    SPDO.USER
SNOWFLAKE_ACCOUNT                 yj90898.sa-east-1.aws
SNOWFLAKE_ROLE                    SPDO_TECH_LEAD_ROLE
SNOWFLAKE_WAREHOUSE               SPDO_WRH
SNOWFLAKE_PRIVATE_KEY             (PEM inteiro — use o Secrets Manager)
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE  (se a chave for cifrada)
```

## Deploy na AWS

```bash
docker build -t leroy-coletor .
docker run --rm \
  -e SNOWFLAKE_USER -e SNOWFLAKE_ACCOUNT -e SNOWFLAKE_ROLE \
  -e SNOWFLAKE_WAREHOUSE -e SNOWFLAKE_PRIVATE_KEY \
  leroy-coletor
```

A execução leva poucos minutos e não mantém estado, então **ECS Fargate com
EventBridge Scheduler** é o encaixe natural (uma tarefa por dia). A chave RSA
vai no Secrets Manager, injetada como variável de ambiente pela task definition.
Lambda também caberia no tempo, mas o Parquet intermediário fica mais
confortável em Fargate.

Preço de varejo muda em ciclo diário — uma execução por dia cobre qualquer uso
e mantém a carga irrelevante para a Leroy.

## Modelo de dados

Grão: **SKU × REGIÃO**. `UF` é coluna derivada.

São Paulo tem 8 regiões comerciais com preços independentes (Grande SP,
Campinas, Sorocaba, Ribeirão Preto, Vale do Paraíba, S. J. do Rio Preto,
Baixada Santista, Bauru). Agregar para UF perderia essa variação, então a
agregação fica a critério de quem consulta:

```sql
-- preço por UF, quando há mais de uma região na mesma UF
SELECT UF, COD_PRODUTO, MIN(PRECO_PROMOCIONAL), MAX(PRECO_PROMOCIONAL)
FROM BASES_SPDO.DB_SANDBOX.TBL_LEROY_TESTE
GROUP BY UF, COD_PRODUTO;
```

**27 regiões cobrindo 17 UFs.** A Leroy não tem preço regional para as 27 UFs
do Brasil. A chave `outros` (balde nacional sem UF) é descartada, conforme
definido. UFs cobertas: AC, AL, BA, CE, DF, ES, GO, MG, MS, MT, PR, RJ, RN, RO,
RS, SC, SP.

Se a Leroy abrir uma região nova, ela aparece no log como
`REGIÕES NOVAS não mapeadas` — é preciso atualizar `src/regioes.py`. A coleta
não a inclui silenciosamente.

## Vendedores: Leroy e marketplace

Coleta **todo o catálogo**, produtos próprios e de marketplace, com o vendedor
identificado:

| Coluna | Conteúdo |
|---|---|
| `SELLER_PRINCIPAL` | nome do vendedor (`Leroy Merlin` ou o seller) |
| `SELLER_TIPO` | `LEROY` ou `MARKETPLACE` |
| `SELLER_ID` | ID interno do seller de marketplace |
| `SELLERS_TODOS` | todos os vendedores do produto, separados por `|` |
| `QTD_SELLERS` | quantos vendedores ofertam o produto |
| `TEM_MARKETPLACE` | o produto também é ofertado por terceiros |

Quando a Leroy está entre os vendedores, ela é a principal: a estrutura de
preço regional é dela.

O grão **não** inclui o vendedor. O índice traz um preço por região, não um
preço por vendedor — explodir a linha por vendedor duplicaria o mesmo preço e
inventaria uma relação que o dado de origem não sustenta.

### Por que não há coluna de CNPJ

**O CNPJ dos vendedores de marketplace não é obtenível pelas fontes públicas.**
Verificamos três rotas:

1. O índice Algolia não tem campo de CNPJ nem de razão social — só nome
   fantasia e um ID interno.
2. Não existe índice de sellers acessível com a chave pública
   (`production_sellers` e variantes retornam 404).
3. A página de um produto de marketplace traz apenas os CNPJs da própria
   Leroy, no rodapé. O bloco "Vendido e entregue por" é renderizado no
   cliente, atrás do DataDome.

O CNPJ da Leroy (**01.438.784/0001-05**, Leroy Merlin Cia Brasileira de
Bricolagem, lido do rodapé do site) seria a única coluna preenchida — e é
constante, já derivável de `SELLER_TIPO = 'LEROY'`. Por isso a coluna foi
retirada da tabela; o valor fica como constante em `src/sellers.py`.

Para enriquecer por fora, `src/vendedores.py` gera a planilha dos vendedores
distintos com colunas `CNPJ` e `RAZAO_SOCIAL` em branco, ordenada por número de
SKUs (a Leroy já vem resolvida):

```bash
python -m src.vendedores            # -> data/vendedores.csv
```

Duas rotas de preenchimento: consulta por nome fantasia em API pública de CNPJ
(BrasilAPI, ReceitaWS) — viável para ~1.000 vendedores, mas o casamento é
impreciso e exige revisão; ou coleta assistida por navegador das páginas dos
maiores vendedores — precisa, porém manual e sujeita ao DataDome.

## Quando a chave Algolia for rotacionada

`src/main.py` valida as credenciais antes de qualquer coleta e falha com
mensagem explícita. Para renovar:

```bash
pip install curl_cffi
python -m src.remine
```

Se o DataDome bloquear também o `remine`, o passo manual leva um minuto:
abra o site no Chrome, DevTools → Network, filtre por `algolia`, e copie
`X-Algolia-Application-Id` e `X-Algolia-API-Key` dos cabeçalhos de qualquer
requisição. Depois defina `ALGOLIA_APP_ID` e `ALGOLIA_API_KEY` no ambiente.

## Estrutura

```
src/regioes.py         mapa região → UF (27 regiões, 17 UFs)
src/extract.py         sitemaps do CDN + getObjects do Algolia (streaming)
src/transform.py       normalização para linhas SKU × região
src/load_snowflake.py  DDL + carga idempotente por dia
src/sellers.py         identificação do vendedor (nome, tipo, CNPJ)
src/main.py            orquestração e CLI
src/recarrega.py       recarga na Snowflake a partir de um Parquet já coletado
src/vendedores.py      exporta a lista de vendedores para enriquecer o CNPJ
src/remine.py          renovação da chave pública (raro)
```

## Notas de conformidade

- O `robots.txt` permite as páginas de produto e declara os sitemaps
  abertamente. A rota usada aqui não toca os caminhos bloqueados
  (`/search`, `/*?filter`, `/promocoes`).
- Preço, EAN e descrição de produto não são dados pessoais — não há LGPD.
- Os **Termos de Uso do site** podem restringir coleta automatizada
  independentemente do `robots.txt`. Vale ler antes de operacionalizar,
  especialmente para uso comercial.
- O `User-Agent` padrão é identificável e traz um contato. Ajuste em
  `LEROY_USER_AGENT` se necessário.
