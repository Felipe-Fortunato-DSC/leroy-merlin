FROM python:3.12-slim

# tzdata para que EXTRAIDO_EM saia coerente com o fuso de operação
ENV TZ=America/Sao_Paulo \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Usuário sem privilégios
RUN useradd --create-home --uid 10001 coletor && \
    mkdir -p /app/data && chown -R coletor:coletor /app
USER coletor

# Sem credencial embutida: a conexão vem de variáveis de ambiente.
#   SNOWFLAKE_USER, SNOWFLAKE_ACCOUNT, SNOWFLAKE_ROLE, SNOWFLAKE_WAREHOUSE
#   SNOWFLAKE_PRIVATE_KEY        (PEM inteiro — use Secrets Manager)
#   SNOWFLAKE_PRIVATE_KEY_PASSPHRASE (se a chave for cifrada)
# Opcionais, se a Leroy rotacionar a chave pública do índice:
#   ALGOLIA_APP_ID, ALGOLIA_API_KEY, ALGOLIA_INDEX

ENTRYPOINT ["python", "-m", "src.main"]
