"""Coleta do catálogo Leroy Merlin.

Duas fontes, ambas públicas e ambas FORA do domínio protegido por DataDome:

  1. cdn.leroymerlin.com.br  — sitemaps XML, dão a lista canônica de IDs de produto
  2. <app>-dsn.algolia.net   — índice de busca que o próprio site consome

O pipeline nunca requisita www.leroymerlin.com.br, por isso o CAPTCHA do DataDome
não o afeta.

A recuperação usa o endpoint `getObjects` do Algolia, que aceita 1.000 objectIDs
por chamada. Isso torna a coleta determinística: pedimos exatamente os IDs do
sitemap e sabemos, ID a ID, o que voltou e o que não voltou. Não há paginação
nem dependência de `nbHits` (que o Algolia devolve aproximado em resultados
grandes).
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)


@dataclass
class RespostaBruta:
    """Uma resposta HTTP com os bytes crus preservados, para evidência.

    Os bytes vêm junto com os registros já parseados de propósito: o hash da
    evidência tem de ser calculado sobre o que chegou do servidor, e qualquer
    reserialização a partir de `registros` produziria um documento diferente.

    `corpo` é liberado assim que a evidência é registrada — segurar os bytes de
    803 lotes somaria mais de 100 MB sem necessidade.
    """
    registros: list[dict]
    corpo: bytes
    endpoint: str
    parametros: dict
    servidor_date: str | None = None
    dt_requisicao: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def libera(self) -> None:
        self.corpo = b""


# --- Credenciais públicas do índice -----------------------------------------
# Publicadas de propósito nos bundles JS do site (é assim que busca no navegador
# funciona). A chave é somente-busca e não expira (validity=0).
# Se a Leroy rotacionar, use `python -m src.remine` para extrair as novas.
ALGOLIA_APP_ID = os.getenv("ALGOLIA_APP_ID", "1CF3ZT43ZU")
ALGOLIA_API_KEY = os.getenv("ALGOLIA_API_KEY", "28e054533dcdd3d71379fc3f38e78f1e")
ALGOLIA_INDEX = os.getenv("ALGOLIA_INDEX", "production_products")

SITEMAP_TMPL = "https://cdn.leroymerlin.com.br/sitemaps/produtos_{n}.xml"

USER_AGENT = os.getenv(
    "LEROY_USER_AGENT",
    "leroy-price-collector/1.0 (+contato: fortunato.tec@outlook.com)",
)

# Campos pedidos ao Algolia. Pedir só o necessário reduz muito o payload.
ATTRS = [
    "objectID",
    "product_id",
    "name",
    "shortName",
    "eans",
    "unit",
    "url",
    "regionalAttributes",
    "hierarchicalCategories",
    "boitataFacets",
    "marketplaceSellers",
]

BATCH = 1000  # teto do getObjects

_RE_LOC = re.compile(r"<loc>(.*?)</loc>")
_RE_ID_URL = re.compile(r"_(\d+)$")  # .../nome-do-produto_92236424


class ExtracaoError(RuntimeError):
    pass


def _client(timeout: float = 90.0) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )


def _algolia_client(timeout: float = 90.0) -> httpx.Client:
    return httpx.Client(
        headers={
            "X-Algolia-Application-Id": ALGOLIA_APP_ID,
            "X-Algolia-API-Key": ALGOLIA_API_KEY,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 1. IDs de produto, a partir dos sitemaps do CDN
# ---------------------------------------------------------------------------
def _existe_sitemap(c: httpx.Client, n: int) -> bool:
    """Um sitemap inexistente devolve 403 (não 404) no CDN.

    Precisamos separar 'não existe' de 'fomos bloqueados': a resposta de objeto
    ausente é curta, enquanto um bloqueio real traz a página do DataDome.
    """
    r = c.get(SITEMAP_TMPL.format(n=n))
    if r.status_code == 200:
        return True
    if r.status_code in (403, 404):
        corpo = r.text[:2000].lower()
        if "datadome" in corpo or "captcha" in corpo or "enable js" in corpo:
            raise ExtracaoError(
                f"o CDN devolveu página de bloqueio em produtos_{n}.xml — "
                "algo mudou na proteção do site. Investigue antes de reexecutar."
            )
        return False
    raise ExtracaoError(f"produtos_{n}.xml devolveu HTTP {r.status_code}")


def _descobre_sitemaps(max_sitemaps: int) -> list[int]:
    """Quantos produtos_N.xml existem hoje. Não fixamos o número: ele cresce."""
    existentes: list[int] = []
    falhas_seguidas = 0
    with _client(60) as c:
        for n in range(1, max_sitemaps + 1):
            if _existe_sitemap(c, n):
                existentes.append(n)
                falhas_seguidas = 0
            else:
                falhas_seguidas += 1
                if falhas_seguidas >= 2 and existentes:
                    break
    if not existentes:
        raise ExtracaoError("nenhum sitemap de produto encontrado no CDN")
    if existentes[-1] == max_sitemaps:
        log.warning(
            "todos os %d sitemaps sondados existem — pode haver mais. "
            "Aumente max_sitemaps.", max_sitemaps,
        )
    log.info("sitemaps de produto encontrados: %d (produtos_1..%d)",
             len(existentes), existentes[-1])
    return existentes


def _baixa_sitemap(n: int, tentativas: int = 3) -> RespostaBruta:
    url = SITEMAP_TMPL.format(n=n)
    with _client(120) as c:
        for i in range(tentativas):
            try:
                t0 = datetime.now(timezone.utc)
                r = c.get(url)
                r.raise_for_status()
                return RespostaBruta(
                    registros=[{"loc": u} for u in _RE_LOC.findall(r.text)],
                    corpo=r.content,
                    endpoint=url,
                    parametros={"sitemap": n},
                    servidor_date=r.headers.get("date"),
                    dt_requisicao=t0,
                )
            except Exception as exc:  # noqa: BLE001
                if i == tentativas - 1:
                    raise ExtracaoError(f"sitemap produtos_{n}.xml falhou: {exc}") from exc
                time.sleep(2 * (i + 1))
    raise ExtracaoError(f"sitemap produtos_{n}.xml esgotou as tentativas")


def coleta_ids(max_sitemaps: int = 60, workers: int = 6,
               ao_receber: Callable[[RespostaBruta], None] | None = None) -> list[str]:
    """IDs de produto do sitemap, em ordem estável e sem duplicatas.

    `ao_receber` é chamado com cada sitemap baixado, antes de os IDs serem
    extraídos — é onde o chamador registra a evidência. O sitemap é
    proveniência de verdade: é ele que define QUAIS produtos foram pedidos, e
    sem ele um auditor não distingue "produto ausente do catálogo" de
    "produto que deixamos de pedir".
    """
    t0 = time.time()
    numeros = _descobre_sitemaps(max_sitemaps)

    ids: list[str] = []
    vistos: set[str] = set()
    with ThreadPoolExecutor(workers) as ex:
        resultados = list(ex.map(_baixa_sitemap, numeros))

    for n, resp in zip(numeros, resultados):
        # Evidência primeiro, dado depois — a mesma ordem da coleta de preços.
        if ao_receber is not None:
            ao_receber(resp)
        urls = [r["loc"] for r in resp.registros]
        resp.libera()
        for u in urls:
            m = _RE_ID_URL.search(u)
            if m and m.group(1) not in vistos:
                vistos.add(m.group(1))
                ids.append(m.group(1))
        log.info("  produtos_%d.xml: %d urls", n, len(urls))

    if not ids:
        raise ExtracaoError("nenhum ID de produto obtido dos sitemaps")

    log.info("IDs únicos coletados: %d (%.1fs)", len(ids), time.time() - t0)
    return ids


# ---------------------------------------------------------------------------
# 2. Registros, a partir do índice Algolia
# ---------------------------------------------------------------------------
def _get_objects(client: httpx.Client, lote: list[str], tentativas: int = 4) -> RespostaBruta:
    body = {
        "requests": [
            {"indexName": ALGOLIA_INDEX, "objectID": i, "attributesToRetrieve": ATTRS}
            for i in lote
        ]
    }
    url = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/*/objects"
    for i in range(tentativas):
        try:
            # O instante é medido ANTES do envio: é o que a evidência afirma
            # sobre quando o preço foi observado. Medir depois embutiria a
            # latência da rede no carimbo.
            t0 = datetime.now(timezone.utc)
            r = client.post(url, json=body)
            if r.status_code == 429:
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
            return RespostaBruta(
                # posições sem correspondência voltam como null
                registros=[h for h in r.json().get("results", []) if h],
                corpo=r.content,            # bytes crus, nunca r.text
                endpoint=url,
                parametros={
                    "indice": ALGOLIA_INDEX,
                    "atributos": list(ATTRS),
                    "object_ids": list(lote),
                },
                servidor_date=r.headers.get("date"),
                dt_requisicao=t0,
            )
        except Exception as exc:  # noqa: BLE001
            if i == tentativas - 1:
                raise ExtracaoError(f"getObjects falhou após {tentativas} tentativas: {exc}") from exc
            time.sleep(2 ** i)
    raise ExtracaoError("getObjects esgotou as tentativas sem resposta utilizável")


def valida_credenciais() -> None:
    """Canário: confirma que App ID, chave e índice ainda respondem.

    Falha cedo e com mensagem clara, em vez de produzir uma carga vazia.
    """
    with _algolia_client(30) as c:
        r = c.post(
            f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query",
            json={"params": "query=&hitsPerPage=1"},
        )
    if r.status_code != 200:
        raise ExtracaoError(
            f"índice Algolia não respondeu (HTTP {r.status_code}). "
            f"A chave pública pode ter sido rotacionada — rode `python -m src.remine` "
            f"para extrair App ID/chave atuais dos bundles do site. Resposta: {r.text[:200]}"
        )
    hits = r.json().get("hits") or []
    if not hits or "regionalAttributes" not in hits[0]:
        raise ExtracaoError(
            "índice respondeu mas sem o campo `regionalAttributes` — o schema mudou. "
            "Reveja src/transform.py antes de carregar."
        )
    log.info("credenciais Algolia validadas (app=%s índice=%s)", ALGOLIA_APP_ID, ALGOLIA_INDEX)


def itera_registros(ids: list[str], workers: int = 4, janela: int | None = None):
    """Gera os registros em lotes, à medida que chegam, COM contrapressão.

    A contrapressão é o ponto. `ThreadPoolExecutor.map` sobre a lista inteira
    submete os 800+ lotes de uma vez: os workers baixam do Algolia muito mais
    rápido do que o consumidor processa (normalizar, gravar Parquet, subir para
    a Snowflake) e os resultados se empilham em memória — medimos 6,3 GB de
    heap e a coleta degradando de 1,4 s/lote para 4,1 s/lote por pressão de GC.

    Aqui mantemos no máximo `janela` lotes em voo. A memória fica constante e o
    ritmo não degrada ao longo da execução.

    Produz (RespostaBruta, n_do_lote, total_de_lotes). A RespostaBruta carrega
    os bytes originais para que o consumidor registre a evidência ANTES de
    derivar qualquer preço.
    """
    lotes = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    janela = janela or workers * 2
    log.info("buscando %d IDs em %d lotes de até %d (janela=%d)",
             len(ids), len(lotes), BATCH, janela)
    t0 = time.time()
    n = 0

    with _algolia_client() as c:
        with ThreadPoolExecutor(workers) as ex:
            pendentes: deque = deque()
            proximo = 0

            def enfileira():
                nonlocal proximo
                while len(pendentes) < janela and proximo < len(lotes):
                    pendentes.append(ex.submit(_get_objects, c, lotes[proximo]))
                    proximo += 1

            enfileira()
            while pendentes:
                parte = pendentes.popleft().result()
                n += 1
                # Só repõe depois de consumir: é isso que segura o produtor.
                enfileira()
                yield parte, n, len(lotes)
                if n % 100 == 0 or n == len(lotes):
                    log.info("  lote %d/%d (%.0fs)", n, len(lotes), time.time() - t0)

