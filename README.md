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
400 mil linhas — backup e insumo para recarga sem recoletar) e
`data/ultima_execucao.json` com as estatísticas. Amostras gravam em
`data/leroy_amostra_AAAAMMDD_pNNN.parquet`, para nunca sobrescrever a coleta
completa do dia; partes que sobraram de uma execução anterior do mesmo dia são
apagadas ao fim.

Para recarregar na Snowflake sem recoletar (sempre o dia inteiro — a carga
apaga a data antes de inserir, então subir uma parte só apagaria as demais):

```bash
python -m src.recarrega                    # coleta completa mais recente
python -m src.recarrega --data 20261008
```

A coleta, a gravação e a carga acontecem **em blocos**: o catálogo completo
passa de 20 milhões de linhas e não caberia num único DataFrame.

A carga é **idempotente por dia de coleta** (a data de Brasília em que a
execução começou): antes de inserir, remove os preços e as evidências desse
dia. Rodar duas vezes no mesmo dia não duplica. Isso vale também para `--amostra`: uma amostra carregada
na Snowflake substitui a carga completa do dia — para testar, use
`--parquet-only`.

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
docker build --build-arg VERSAO=$(git rev-parse --short=12 HEAD) -t leroy-coletor .
docker run --rm \
  -e SNOWFLAKE_USER -e SNOWFLAKE_ACCOUNT -e SNOWFLAKE_ROLE \
  -e SNOWFLAKE_WAREHOUSE -e SNOWFLAKE_PRIVATE_KEY \
  -v /caminho/persistente/evidencias:/app/evidencias \
  leroy-coletor
```

A execução leva cerca de 40 minutos, então **ECS Fargate com EventBridge
Scheduler** é o encaixe natural (uma tarefa por dia) — Lambda não cabe no
limite de 15 minutos. A chave RSA vai no Secrets Manager, injetada como
variável de ambiente pela task definition.

O container **não** é sem estado: `/app/evidencias` guarda os brutos, os
índices e a chave do selo, e precisa ser um volume persistente (EFS). Num
disco efêmero as evidências sumiriam com a tarefa. `VERSAO` grava o commit em
cada evidência, já que a imagem não leva o `.git`.

Preço de varejo muda em ciclo diário — uma execução por dia cobre qualquer uso
e mantém a carga irrelevante para a Leroy.

## Camada de evidências

Cada preço carrega a prova técnica de quando e de onde veio. O pacote
`evidencia/` é independente de `src/` e serve a qualquer robô de coleta.

### Na Snowflake: uma coluna e uma tabela

A tabela de preços mantém as 23 colunas originais e ganha **uma**:
`ID_EVIDENCIA`, a resposta HTTP que originou a linha (uma resposta traz até
1.000 produtos × 27 regiões; todas as linhas dela apontam para a mesma
evidência).

`TBL_EVIDENCIAS_TESTE` tem seis colunas e uma linha por resposta HTTP, mais
uma linha de **selo** por dia:

| Coluna | Evidência | Selo (`EV-LM-AAAAMMDD-SELO`) |
|---|---|---|
| `ID_EVIDENCIA` | `EV-LM-20261008-000123` | `EV-LM-20261008-SELO` |
| `DT_REQUISICAO` | instante real da requisição, medido antes do envio | quando foi assinado |
| `ENDPOINT` | URL requisitada | `selo:ed25519:{impressão da chave}` |
| `HASH_BRUTO` | SHA-256 dos bytes exatos recebidos | o hash do dia |
| `DIGEST_LINHAS` | resumo das linhas de preço derivadas desta resposta | — |
| `ASSINATURA` | — | Ed25519 sobre o hash do dia |

E a validação é uma view sobre ela, `VW_AUDITORIA_EVIDENCIAS`, que dá o
`STATUS` de cada evidência:

| STATUS | Significa |
|---|---|
| `integra` | linhas conferem e o dia está selado com o hash certo |
| `linhas_alteradas` | UPDATE, INSERT ou DELETE em preços desta evidência |
| `evidencias_alteradas` | a tabela de evidências não bate com o hash selado |
| `dia_nao_selado` | sem selo — carga em andamento ou que não conferiu |
| `linhas_sem_evidencia` | preços cujo `ID_EVIDENCIA` não existe |

```sql
SELECT STATUS, COUNT(*) FROM BASES_SPDO.DB_SANDBOX.VW_AUDITORIA_EVIDENCIAS
WHERE DIA = '20261008' GROUP BY 1;
```

### Como a validação funciona sem guardar hash por linha

A view recalcula o hash de cada linha a partir das próprias colunas
(`SHA2(TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(...)))`, a mesma forma canônica do
Python), agrega por evidência e compara com `DIGEST_LINHAS`. Depois recalcula o
hash do dia sobre as evidências e compara com o do selo:

```
linha de preço ──SQL──▶ DIGEST_LINHAS ──SQL──▶ hash do dia ──Ed25519──▶ ASSINATURA
.gz em disco ───SHA-256──▶ HASH_BRUTO ──────────────▲
```

A equivalência Python ↔ SQL foi conferida na Snowflake real: sobre as 20,85
milhões de linhas de 08/10, o recálculo bate em todas, exceto 27 linhas de um
produto cuja descrição tinha caracteres de controle (carga anterior à limpeza
de `src/texto.py` — ver abaixo). As fórmulas estão fixadas em teste.

**Por que resiste a adulteração:**

1. **`HASH_BRUTO`** pega quem edita o JSON gravado em disco.
2. **`DIGEST_LINHAS`** pega quem altera, insere ou remove um preço na base.
3. **O hash do dia** pega quem mexe na tabela de evidências — inclusive para
   "acompanhar" um preço alterado, ou remover a última evidência do dia.
4. **A assinatura** pega quem recalcula tudo isso: refazer o selo exige a chave
   privada, que não vive junto com os dados. A Snowflake não verifica Ed25519,
   então essa conferência é do `python -m src.auditoria`.

O selo só é emitido depois de a coleta conferir a base contra o disco:
"selado" significa "coletado, carregado e conferido".

> **A chave privada tem de viver fora do diretório de evidências** (variável
> `EVIDENCIA_CHAVES`). Se quem altera a evidência também alcança a chave, o
> selo não prova nada. O `.gitignore` exclui `evidencias/` inteiro.

### Em disco

```
evidencias/leroy_merlin_precos/2026-10-08/160138/   uma pasta por execução
    EV-LM-20261008-000001.json.gz                   corpo bruto, gzip reprodutível
    indice.jsonl                                    as 6 colunas + contexto
    selo.json                                       hash do dia + assinatura
evidencias/_chaves/leroy_merlin_precos.ed25519[.pub]
```

O índice guarda, além das colunas da tabela, o contexto que só o auditor
precisa: parâmetros da requisição (os 1.000 IDs pedidos), cabeçalho `Date` do
servidor, versão do código e tamanho do bruto.

Rodar de novo no mesmo dia cria outra pasta; nada é apagado. A Snowflake fica
com a última carga do dia, e os IDs nunca se repetem entre execuções.

### Uso

```bash
# chave do robô, uma vez
python -m evidencia chave --robo leroy_merlin_precos

# a coleta grava, confere e sela sozinha
python -m src.main

# auditoria completa de um dia: disco, Snowflake e assinatura
python -m src.auditoria --data 2026-10-08

# só o disco; e selar manualmente se a coleta foi interrompida antes
python -m evidencia verificar --robo leroy_merlin_precos --data 2026-10-08
python -m evidencia selar     --robo leroy_merlin_precos --data 2026-10-08

# a DDL completa (também em sql/ddl.sql)
python -m src.load_snowflake
```

Flags de `src.main`: `--sem-evidencia` (diagnóstico; a carga **não** fica
auditável), `--nao-selar`, `--evidencias DIR`.

### Comprovante de coleta

```bash
python -m src.comprovante --produto 92236424 --regiao uberlandia
```

```
Comprovante de Coleta - Leroy Merlin
Tanque 20.000L em Polietileno Acqualimp
Codigo 92236424 | EAN 7898343622613
Preco: R$ 17.349,00 (preco a vista/promocional; preco original R$ 18.456,38)
Vendido por: Leroy Merlin (leroy) | Regiao: Uberlândia/MG
Coletado em: 08/10/2026 as 16:31 (Brasilia)
Origem: https://www.leroymerlin.com.br/tanque-20-000l-em-polietileno-acqualimp_92236424
Integridade: [OK] verificada | Evidencia EV-LM-20261008-000654
```

"Integridade verificada" não é decorativo: o comprovante reabre o `.gz`,
reextrai o preço **com o mesmo parser da coleta** e confere que chega
exatamente à linha gravada. No Power BI, o comprovante é a junção
`TBL_LEROY_TESTE ⋈ TBL_EVIDENCIAS_TESTE` por `ID_EVIDENCIA`, com o `STATUS`
da view.

### Dois horários, e qual usar

`EXTRAIDO_EM` é o início da execução, em UTC, igual para todas as linhas da
carga — numa coleta de 40 minutos, a última linha nasceu 40 minutos depois do
que o campo afirma. **Para o comprovante use `DT_REQUISICAO`**, o instante real
daquela requisição (gravado com o fuso de Brasília).

O **dia da coleta** é a data de Brasília em que a execução começou. É a mesma
chave para o ID das evidências, a pasta, o selo e a carga idempotente na
Snowflake — uma execução que atravessa a meia-noite continua sendo um dia só.

### Volume — medido na coleta completa de 08/10

825 evidências por dia: 803 lotes do Algolia (16,4 MB brutos cada, 464 KB em
gzip — razão de 35:1) e 22 sitemaps (7,3 MB brutos, 952 KB em gzip). Em disco,
**296 MB por dia** (~108 GB por ano). A execução leva cerca de 40 minutos.

Os corpos brutos ficam em disco; a Snowflake guarda só o índice consultável,
porque o que prova é o hash, não o blob.

### Caracteres de controle

O índice da Leroy traz lixo invisível em algumas descrições (`"... Kit Gamer
Skill\x7f\x7f"`). Eles importam porque o `json.dumps` do Python deixa o
`0x7F` cru enquanto o `TO_JSON` da Snowflake o escapa como `\u007f` — e os dois
hashes deixam de bater numa linha que ninguém adulterou. A coleta remove
controles C0 e C1 de todo texto vindo da fonte (`src/texto.py`).

### Testes

```bash
pip install pytest
python -m pytest tests/ -q
```

Cobrem os cenários que a auditoria exige — bruto adulterado, índice adulterado
(data, remoção, remoção da última, digest), selo recalculado sem a chave, preço
divergente — e as armadilhas que os tornariam inúteis: hash de bytes e não de
texto reserializado, dinheiro a 2 casas, execução que atravessa a meia-noite,
IDs que não se repetem e as fórmulas fixadas contra a Snowflake.


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
src/recarrega.py       recarga na Snowflake a partir dos Parquets já coletados
src/constantes.py      caminhos, identidade do robô e nomes dos Parquets
src/texto.py           limpeza de caracteres de controle do texto da fonte
src/vendedores.py      exporta a lista de vendedores para enriquecer o CNPJ
src/remine.py          renovação da chave pública (raro)
src/comprovante.py     comprovante de coleta de um preço, com verificação
src/auditoria.py       auditoria completa de um dia: disco, Snowflake e assinatura

evidencia/hashing.py        SHA-256, forma canônica, digest e hash do dia
evidencia/registro.py       Registrador: registrar (bruto) e fechar (índice)
evidencia/armazenamento.py  layout em disco, gzip reprodutível, índice JSONL
evidencia/selo.py           chaves Ed25519 e selo do dia
evidencia/verificacao.py    conferência do disco: brutos, hash do dia, assinatura
evidencia/snowflake.py      tabela de evidências, view de auditoria, carga
evidencia/__main__.py       CLI: chave, selar, verificar
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
