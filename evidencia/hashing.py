"""Hash e serialização canônica.

Três regras, e o resto do pacote existe para honrá-las:

  1. O SHA-256 do corpo de uma resposta é calculado sobre os BYTES EXATOS
     recebidos, antes de qualquer parse. Reserializar um JSON reordena chaves,
     muda espaços e reescreve escapes Unicode — o hash muda e a evidência
     deixa de provar o que recebemos.

  2. O hash de uma linha tratada usa serialização canônica: chaves ordenadas,
     UTF-8, separadores sem espaço, dinheiro com 2 casas. É a mesma forma que
     a Snowflake produz com TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(...)), e é isso
     que permite recalcular tudo em SQL sem guardar o hash na tabela.

  3. Tudo que é comparado com a Snowflake (`digest_linhas`, `hash_do_dia`) tem
     uma fórmula SQL equivalente, documentada ao lado e coberta por teste.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

CENTAVO = Decimal("0.01")


def sha256_bytes(dados: bytes) -> str:
    """SHA-256 de bytes crus, em hexadecimal minúsculo."""
    if not isinstance(dados, (bytes, bytearray, memoryview)):
        raise TypeError(
            "sha256_bytes exige bytes. Receber str aqui significa que alguém "
            "decodificou a resposta antes do hash — exatamente o que invalida "
            "a evidência."
        )
    return hashlib.sha256(dados).hexdigest()


def sha256_texto(texto: str) -> str:
    return sha256_bytes(texto.encode("utf-8"))


def json_canonico(obj) -> bytes:
    """JSON determinístico: chaves ordenadas, UTF-8, separadores fixos.

    `allow_nan=False` é proposital: NaN e Infinity não são JSON válido e a
    serialização silenciosa deles produziria um documento que outro verificador
    não conseguiria reler.
    """
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def normaliza_valor(v):
    """Põe um valor na forma canônica usada pelo hash de linha.

    `float` vira string com 2 casas porque é dinheiro e é assim que a Snowflake
    o devolve (TO_CHAR de NUMBER(12,2)). Sem isso o hash da coleta não bateria
    com o recalculado a partir da base, por puro ruído de ponto flutuante.
    """
    if hasattr(v, "item"):          # numpy.float64, numpy.bool_, numpy.int64
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    if v is None:
        return None
    if isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        if v != v:                  # NaN: pandas usa para nulo
            return None
        return str(Decimal(str(v)).quantize(CENTAVO))
    if isinstance(v, Decimal):
        return str(v.quantize(CENTAVO))
    if isinstance(v, datetime):
        return v.replace(microsecond=0, tzinfo=None).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def hash_linha(linha: dict) -> str:
    """SHA-256 dos campos de uma linha de preço, em forma canônica.

    Na Snowflake: SHA2(TO_JSON(OBJECT_CONSTRUCT_KEEP_NULL(<colunas>)), 256),
    com dinheiro e momento passados por TO_CHAR (ver snowflake.sql_hash_linha).
    O ID_EVIDENCIA é uma das colunas: repontar a linha para outra evidência
    muda o hash.
    """
    return sha256_bytes(json_canonico({k: normaliza_valor(v) for k, v in linha.items()}))


def digest_linhas(hashes) -> str:
    """Resumo das linhas de uma evidência, independente da ordem.

    Na Snowflake: SHA2(LISTAGG(h, '') WITHIN GROUP (ORDER BY h), 256).

    Qualquer alteração, inserção ou remoção de linha muda o digest. A ordenação
    torna o resultado independente da ordem de leitura da tabela.
    """
    return sha256_bytes("".join(sorted(hashes)).encode("ascii"))


def linha_do_dia(ev: dict) -> str:
    """A representação de uma evidência que entra no hash do dia.

    O endpoint entra pelo seu hash: é o único campo de texto livre, e passá-lo
    cru permitiria forjar fronteiras com '|' ou quebra de linha.
    """
    return "|".join((ev["id_evidencia"], ev["dt_requisicao"], sha256_texto(ev["endpoint"]),
                     ev["hash_bruto"], ev["digest_linhas"] or ""))


def hash_do_dia(evidencias) -> str:
    """Hash de todas as evidências de uma carga, em ordem de ID.

    Na Snowflake:
        SHA2(LISTAGG(ID || '|' || DT || '|' || SHA2(ENDPOINT, 256) || '|' ||
                     HASH_BRUTO || '|' || DIGEST_LINHAS, '\\n')
             WITHIN GROUP (ORDER BY ID_EVIDENCIA), 256)

    É ele que o selo assina. Cobre o conjunto inteiro: alterar, remover,
    inserir ou reordenar qualquer evidência — inclusive a última do dia —
    muda o hash.
    """
    ordenadas = sorted(evidencias, key=lambda e: e["id_evidencia"])
    return sha256_texto("\n".join(linha_do_dia(e) for e in ordenadas))
