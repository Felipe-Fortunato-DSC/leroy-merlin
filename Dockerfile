FROM python:3.12-slim

# Commit do código, gravado em cada evidência (a imagem não leva o .git):
#   docker build --build-arg VERSAO=$(git rev-parse --short=12 HEAD) -t leroy-coletor .
ARG VERSAO=desconhecida

# TZ só afeta os horários do log. EXTRAIDO_EM é gravado em UTC e a data de
# referência das evidências é calculada em America/Sao_Paulo explicitamente.
ENV TZ=America/Sao_Paulo \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COLETOR_VERSAO=$VERSAO \
    EVIDENCIA_RAIZ=/app/evidencias

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY evidencia/ ./evidencia/
COPY src/ ./src/

# Usuário sem privilégios
RUN useradd --create-home --uid 10001 coletor && \
    mkdir -p /app/data /app/evidencias && chown -R coletor:coletor /app
USER coletor

# Sem credencial embutida: a conexão vem de variáveis de ambiente.
#   SNOWFLAKE_USER, SNOWFLAKE_ACCOUNT, SNOWFLAKE_ROLE, SNOWFLAKE_WAREHOUSE
#   SNOWFLAKE_PRIVATE_KEY        (PEM inteiro — use Secrets Manager)
#   SNOWFLAKE_PRIVATE_KEY_PASSPHRASE (se a chave for cifrada)
# Opcionais, se a Leroy rotacionar a chave pública do índice:
#   ALGOLIA_APP_ID, ALGOLIA_API_KEY, ALGOLIA_INDEX
#
# /app/evidencias PRECISA ser um volume persistente (EFS no Fargate): é ali
# que ficam os brutos, os índices e a chave do selo. Num disco efêmero as
# evidências somem com o container e o selo não tem chave para assinar.

ENTRYPOINT ["python", "-m", "src.main"]
