"""Selo da carga: o hash do dia, assinado com Ed25519.

O hash do dia (hashing.hash_do_dia) já denuncia qualquer mudança no conjunto
de evidências — mas quem tem acesso de escrita poderia recalculá-lo depois de
adulterar. A assinatura fecha isso: refazer o selo exige a chave privada, que
não vive junto com os dados.

Ed25519: chave e assinatura pequenas, sem parâmetros para errar, e
determinístico — assinar o mesmo hash duas vezes dá a mesma assinatura.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from . import armazenamento as arm
from .hashing import hash_do_dia, sha256_bytes
from .registro import id_selo, instante_canonico, parse_id

log = logging.getLogger(__name__)


class SeloError(RuntimeError):
    pass


# -- chaves ----------------------------------------------------------------
def gera_chave(raiz: Path, robo: str, sobrescrever: bool = False) -> Path:
    """Cria o par de chaves do robô.

    Recusa sobrescrever por acidente: com a chave nova, os selos já emitidos
    deixam de conferir.
    """
    priv_p = arm.caminho_chave(raiz, robo)
    pub_p = arm.caminho_chave(raiz, robo, publica=True)
    if priv_p.exists() and not sobrescrever:
        raise SeloError(
            f"já existe chave privada em {priv_p}. Sobrescrever invalidaria a "
            f"verificação de todos os selos já assinados."
        )
    priv_p.parent.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    priv_p.write_bytes(priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    pub_p.write_bytes(priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    try:                            # no Windows é no-op, mas não custa
        priv_p.chmod(0o600)
    except OSError:
        pass
    return priv_p


def carrega_privada(raiz: Path, robo: str) -> Ed25519PrivateKey:
    p = arm.caminho_chave(raiz, robo)
    if not p.exists():
        raise SeloError(f"chave privada ausente em {p}. Gere uma com "
                        f"`python -m evidencia chave --robo {robo}`.")
    k = serialization.load_pem_private_key(p.read_bytes(), password=None)
    if not isinstance(k, Ed25519PrivateKey):
        raise SeloError(f"{p} não é uma chave Ed25519")
    return k


def carrega_publica(raiz: Path, robo: str) -> Ed25519PublicKey:
    p = arm.caminho_chave(raiz, robo, publica=True)
    if not p.exists():
        raise SeloError(f"chave pública ausente em {p}")
    k = serialization.load_pem_public_key(p.read_bytes())
    if not isinstance(k, Ed25519PublicKey):
        raise SeloError(f"{p} não é uma chave pública Ed25519")
    return k


def impressao(pub: Ed25519PublicKey) -> str:
    """Identifica a chave (16 hex do SHA-256 da chave crua) sem expor o PEM."""
    cru = pub.public_bytes(encoding=serialization.Encoding.Raw,
                           format=serialization.PublicFormat.Raw)
    return sha256_bytes(cru)[:16]


# -- selo ------------------------------------------------------------------
def _mensagem(selo: dict) -> bytes:
    """O que é assinado: o ID do selo (robô + dia) e o hash do dia.

    O ID entra para que a assinatura de um dia não sirva para outro.
    """
    return f"{selo['id_evidencia']}|{selo['hash_dia']}".encode("ascii")


def sela(pasta: Path, raiz: Path, robo: str) -> dict:
    """Assina o hash das evidências de uma execução e grava selo.json."""
    registros = arm.le_indice(pasta)
    if not registros:
        raise SeloError(f"nenhuma evidência em {pasta}: selar um dia vazio "
                        "esconderia uma coleta que não rodou.")
    sigla, dia, _ = parse_id(registros[0]["id_evidencia"])
    priv = carrega_privada(raiz, robo)
    selo = {
        "id_evidencia": id_selo(sigla, dia),
        "dt_selo": instante_canonico(),
        "n_evidencias": len(registros),
        "hash_dia": hash_do_dia(registros),
        "chave": impressao(priv.public_key()),
    }
    selo["assinatura"] = base64.b64encode(priv.sign(_mensagem(selo))).decode("ascii")
    arm.grava_json(pasta / arm.SELO, selo)
    log.info("selo %s: %d evidências, hash do dia %s",
             selo["id_evidencia"], selo["n_evidencias"], selo["hash_dia"][:16])
    return selo


def assinatura_confere(selo: dict, pub: Ed25519PublicKey) -> bool:
    try:
        pub.verify(base64.b64decode(selo["assinatura"]), _mensagem(selo))
        return True
    except (InvalidSignature, KeyError, ValueError):
        return False
