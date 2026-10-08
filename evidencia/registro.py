"""Registro de evidências — o núcleo do pacote.

Uma evidência prova que um corpo de resposta específico chegou de um endpoint
específico num instante específico. Duas etapas, nesta ordem:

    reg = Registrador(robo="leroy_merlin_precos", sigla="LM")
    ev = reg.registrar(corpo=r.content, endpoint=url, dt_requisicao=t0)  # .gz no disco
    linhas = parse(...)                                                    # preços
    ev = reg.fechar(ev, [hash_linha(l) for l in linhas])                  # índice

O bruto vai para o disco ANTES de qualquer preço ser derivado dele; o índice
recebe a evidência já com o digest dos preços. Se o processo morrer no meio,
sobra um .gz órfão — nunca um preço sem prova.

Não há cadeia de hashes: o selo do dia (selo.py) assina o hash do conjunto
inteiro, o que já detecta remoção, inserção, alteração e reordenação.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import armazenamento as arm
from .armazenamento import BRASILIA
from .hashing import digest_linhas, sha256_bytes

_RE_ID = re.compile(r"^EV-([A-Z0-9]{1,8})-(\d{8})-(\d{6}|SELO)$")


class EvidenciaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Evidencia:
    """Uma resposta coletada. Os cinco primeiros campos vão para a Snowflake;
    os demais ficam só no índice em disco, como contexto para o auditor."""
    id_evidencia: str
    dt_requisicao: str              # UTC canônico: 2026-10-08T19:01:38.106Z
    endpoint: str
    hash_bruto: str
    digest_linhas: str | None = None
    n_linhas: int = 0
    tamanho_bruto: int = 0
    servidor_date: str | None = None
    versao: str = ""
    parametros: dict = field(default_factory=dict)


def instante_canonico(dt: datetime | None = None) -> str:
    """Instante em UTC com milissegundos — a forma que entra no hash do dia.

    Na Snowflake: TO_CHAR(CONVERT_TIMEZONE('UTC', DT), 'YYYY-MM-DD"T"HH24:MI:SS.FF3"Z"').
    """
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def hoje_brt() -> date:
    """Data de hoje em Brasília — a data de referência das coletas."""
    return datetime.now(BRASILIA).date()


def parse_id(id_evidencia: str) -> tuple[str, date, int | None]:
    """'EV-LM-20260914-000123' -> ('LM', date(2026, 9, 14), 123). Selo: seq None."""
    m = _RE_ID.match(id_evidencia)
    if not m:
        raise EvidenciaError(f"ID de evidência fora do padrão: {id_evidencia!r}")
    sigla, dia, seq = m.groups()
    return sigla, datetime.strptime(dia, "%Y%m%d").date(), None if seq == "SELO" else int(seq)


def id_selo(sigla: str, dia: date) -> str:
    return f"EV-{sigla}-{dia:%Y%m%d}-SELO"


@lru_cache(maxsize=4)
def versao_codigo(repo: str | None = None) -> str:
    """Commit git do código que coletou, com marca `-sujo` se havia alteração.

    A reextração de um preço a partir do bruto só bate com o mesmo parser.
    Sem git (imagem Docker), vale a variável COLETOR_VERSAO.
    """
    if os.getenv("COLETOR_VERSAO"):
        return os.environ["COLETOR_VERSAO"]
    caminho = Path(repo) if repo else Path(__file__).resolve().parent.parent
    try:
        git = ["git", "-C", str(caminho)]
        commit = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=10, check=True).stdout.strip()[:12]
        sujo = subprocess.run([*git, "status", "--porcelain"], capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()
        return f"{commit}-sujo" if sujo else commit
    except Exception:          # noqa: BLE001 — sem git, a coleta não pode parar
        return "desconhecida"


class Registrador:
    """Grava as evidências de UMA execução de um robô, numa pasta própria.

    `dia` é a data de referência da coleta (Brasília), a mesma em todas as
    evidências da execução — inclusive quando ela atravessa a meia-noite. É a
    chave que une ID, pasta, selo e a carga do dia na Snowflake.
    """

    def __init__(self, robo: str, sigla: str, dia: date | None = None,
                 raiz: Path | str | None = None, versao: str | None = None) -> None:
        self.robo = robo
        self.sigla = sigla.upper()
        self.dia = dia or hoje_brt()
        self.raiz = Path(raiz) if raiz else arm.RAIZ_PADRAO
        self.versao = versao or versao_codigo()
        self._seq = arm.ultimo_seq(self.raiz, robo, self.dia)
        self.pasta = arm.nova_execucao(self.raiz, robo, self.dia)
        self._lock = threading.Lock()

    def registrar(self, corpo: bytes, endpoint: str, dt_requisicao: datetime | None = None,
                  parametros: dict | None = None, servidor_date: str | None = None) -> Evidencia:
        """Grava o bruto e devolve a evidência, ainda sem o digest dos preços.

        `dt_requisicao` deve ser medido ANTES do envio: é o que a evidência
        afirma sobre quando o preço foi observado.
        """
        if not isinstance(corpo, (bytes, bytearray)):
            raise EvidenciaError(
                "corpo precisa ser bytes (use r.content, não r.text): o hash "
                "tem de ser dos bytes exatos recebidos."
            )
        with self._lock:
            self._seq += 1
            id_ev = f"EV-{self.sigla}-{self.dia:%Y%m%d}-{self._seq:06d}"
            arm.grava_bruto(arm.caminho_bruto(self.pasta, id_ev), bytes(corpo))
        return Evidencia(
            id_evidencia=id_ev,
            dt_requisicao=instante_canonico(dt_requisicao),
            endpoint=endpoint,
            hash_bruto=sha256_bytes(bytes(corpo)),
            tamanho_bruto=len(corpo),
            servidor_date=servidor_date,
            versao=self.versao,
            parametros=parametros or {},
        )

    def fechar(self, ev: Evidencia, hashes_linhas) -> Evidencia:
        """Anota o digest dos preços derivados de `ev` e a grava no índice.

        Evidência sem preço (sitemap, lote vazio) também é fechada, com o
        digest do vazio — "não gera preço" é um fato, não uma ausência.
        """
        hashes = list(hashes_linhas)
        ev = replace(ev, digest_linhas=digest_linhas(hashes), n_linhas=len(hashes))
        arm.anexa_indice(self.pasta, asdict(ev))
        return ev
