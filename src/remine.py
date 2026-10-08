"""Re-extrai App ID, chave de busca e índice Algolia dos bundles do site.

Só é necessário se a Leroy rotacionar a chave pública — o extrator avisa com
mensagem explícita quando isso acontece (`valida_credenciais`).

    python -m src.remine

Este é o ÚNICO ponto do projeto que toca www.leroymerlin.com.br, e faz uma
única requisição. Como esse domínio é protegido por DataDome, usa curl_cffi
para replicar o handshake TLS do Chrome. Se falhar, o passo manual está
documentado no README.
"""
from __future__ import annotations

import re
import sys

URL_PRODUTO = "https://www.leroymerlin.com.br/cimentinho-branco-1kg-assentamais_89880700"
PERFIS = ["chrome110", "chrome116", "chrome120", "chrome124", "chrome99"]


def _html_do_site() -> str:
    try:
        from curl_cffi import requests
    except ImportError:
        sys.exit(
            "curl_cffi não instalado. Rode:  pip install curl_cffi\n"
            "Ou siga o passo manual do README (DevTools > Network)."
        )
    for perfil in PERFIS:
        try:
            r = requests.Session(impersonate=perfil).get(
                URL_PRODUTO, timeout=40, headers={"Accept-Language": "pt-BR,pt;q=0.9"}
            )
            if r.status_code == 200 and len(r.text) > 50_000:
                print(f"  página obtida com impersonate={perfil}")
                return r.text
            print(f"  {perfil}: HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {perfil}: {type(exc).__name__}")
    sys.exit(
        "Nenhum perfil passou pelo DataDome.\n"
        "Passo manual: abra o site no Chrome, DevTools > Network, filtre por "
        "'algolia', e copie X-Algolia-Application-Id e X-Algolia-API-Key de "
        "qualquer requisição. Depois exporte:\n"
        "  set ALGOLIA_APP_ID=...\n  set ALGOLIA_API_KEY=..."
    )


def main() -> int:
    import httpx

    from .extract import USER_AGENT

    print("1. baixando uma página de produto...")
    html = _html_do_site()

    app = re.search(r"https://([A-Z0-9]{8,12})-dsn\.algolia\.net", html)
    if not app:
        sys.exit("App ID do Algolia não encontrado no HTML — o site mudou de arquitetura.")
    app_id = app.group(1)
    print(f"   App ID: {app_id}")

    print("2. varrendo os bundles JS do CDN (sem proteção)...")
    chunks = set(re.findall(r"https://cdn\.leroymerlin\.com\.br/[^\"']+\.js", html))
    print(f"   {len(chunks)} bundles")

    chaves: set[str] = set()
    indices: set[str] = set()
    with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}) as c:
        for u in chunks:
            try:
                body = c.get(u).text
            except Exception:  # noqa: BLE001
                continue
            chaves |= set(re.findall(r"[\"']([0-9a-f]{32})[\"']", body))
            indices |= set(re.findall(r"[\"'](production_[a-z_]+)[\"']", body))

    print(f"\n   chaves candidatas: {sorted(chaves)}")
    print(f"   índices: {sorted(indices)}")

    print("\n3. testando cada chave contra o índice...")
    alvo = "production_products" if "production_products" in indices else (
        sorted(indices)[0] if indices else "production_products")
    for k in sorted(chaves):
        try:
            r = httpx.post(
                f"https://{app_id}-dsn.algolia.net/1/indexes/{alvo}/query",
                headers={"X-Algolia-Application-Id": app_id, "X-Algolia-API-Key": k,
                         "Content-Type": "application/json"},
                json={"params": "query=&hitsPerPage=1"}, timeout=30,
            )
            if r.status_code == 200 and (r.json().get("hits") or []):
                print(f"\n   FUNCIONA: {k}")
                print("\nDefina no ambiente (ou edite src/extract.py):")
                print(f"  ALGOLIA_APP_ID={app_id}")
                print(f"  ALGOLIA_API_KEY={k}")
                print(f"  ALGOLIA_INDEX={alvo}")
                return 0
        except Exception:  # noqa: BLE001
            pass
    print("Nenhuma chave candidata funcionou.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
