"""main.py : Phase 5. API FastAPI de concentration des holders ERC-20.

Lancer :
    set ETHERSCAN_API_KEY=...            (PowerShell : $env:ETHERSCAN_API_KEY="...")
    uvicorn main:app --reload
Docs interactives : http://127.0.0.1:8000/docs

Les endpoints sont synchrones : la collecte est bloquante. Sans plan Etherscan
Pro, un token avec beaucoup de transferts peut prendre plusieurs minutes à la
première requête (ensuite le cache `data/` rend les appels instantanés).
"""
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

import collector
import known_contracts
import metrics
import visualize

app = FastAPI(title="token-concentration",
              description="Concentration des holders d'un token ERC-20 (Gini, Nakamoto, top N).")

ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
MAX_COMPARE = 10
FRONTEND = Path(__file__).parent / "frontend" / "index.html"


@app.get("/", include_in_schema=False)
def index():
    """Page web (frontend/index.html), servie par la même app : pas de CORS à gérer."""
    return FileResponse(FRONTEND, media_type="text/html")


def _check(token: str, chain: str) -> str:
    """Valide l'adresse et la chaîne. Retourne la chaîne en minuscules."""
    chain = chain.lower()
    if chain not in collector.CHAINS:
        raise HTTPException(400, f"Chaîne inconnue '{chain}'. Disponibles : {', '.join(collector.CHAINS)}")
    if not ADDRESS_RE.fullmatch(token):
        raise HTTPException(400, f"Adresse invalide : {token}")
    return chain


def _load(token: str, chain: str, detect: bool):
    """Collecte (ou cache) + exclusion. Retourne (data, split, balances des retenus)."""
    data = collector.get_token_holders(token, chain)
    split = known_contracts.classify_holders(data, detect=detect)
    if not split["kept"]:
        raise ValueError("Aucun holder retenu après exclusion.")
    scale = 10 ** data["decimals"]
    return data, split, [h["balance"] / scale for h in split["kept"]]


def _summary(data: dict, split: dict, balances: list[float]) -> dict:
    gini = metrics.gini_coefficient(balances)
    top10 = metrics.top_n_share(balances, 10)
    supply = data["total_supply"] or 1
    return {
        "token_address": data["token_address"],
        "chain": data["chain"],
        "symbol": data["symbol"],
        "total_holders": len(split["kept"]),          # holders retenus (base des calculs)
        "holders_raw": len(data["holders"]),          # avant exclusion
        "gini": round(gini, 6),
        "top10_share": round(top10, 6),               # fractions de 0 à 1
        "top50_share": round(metrics.top_n_share(balances, 50), 6),
        "top100_share": round(metrics.top_n_share(balances, 100), 6),
        "nakamoto_coefficient": metrics.nakamoto_coefficient(balances),
        "risk_score": metrics.risk_score(gini, top10),
        "excluded_contracts": [
            {"address": e["address"], "category": e["category"], "label": e["label"],
             "source": e["source"], "share_of_supply": round(e["balance"] / supply, 6)}
            for e in split["excluded"]
        ],
        "contracts_to_review": [
            {"address": r["address"], "label": r["label"],
             "share_of_supply": round(r["balance"] / supply, 6)}
            for r in split["to_review"]
        ],
    }


def _run(token: str, chain: str, detect: bool):
    """_load avec conversion des erreurs métier en erreurs HTTP."""
    try:
        return _load(token, chain, detect)
    except collector.EtherscanError as e:
        raise HTTPException(502, f"Erreur Etherscan : {e}")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/analyze/{token_address}")
def analyze(token_address: str,
            chain: str = "ethereum",
            detect: bool = Query(True, description="Détection auto des pools/contrats (appels réseau)")):
    chain = _check(token_address, chain)
    data, split, balances = _run(token_address, chain, detect)
    return _summary(data, split, balances)


@app.get("/analyze/{token_address}/lorenz")
def analyze_lorenz(token_address: str, chain: str = "ethereum", detect: bool = True):
    chain = _check(token_address, chain)
    data, _, balances = _run(token_address, chain, detect)
    path = collector.DATA_DIR / f"{data['chain']}_{data['token_address']}_lorenz.png"
    visualize.plot_lorenz(balances, path,
                          title=f"Courbe de Lorenz : {data['symbol']} ({data['chain']})")
    return FileResponse(path, media_type="image/png")


@app.get("/analyze/{token_address}/top")
def analyze_top(token_address: str, chain: str = "ethereum", detect: bool = True):
    chain = _check(token_address, chain)
    data, split, _ = _run(token_address, chain, detect)
    path = collector.DATA_DIR / f"{data['chain']}_{data['token_address']}_top.png"
    visualize.plot_top_holders(split["kept"], path, n=20,
                               title=f"Les 20 plus gros détenteurs : {data['symbol']} ({data['chain']})")
    return FileResponse(path, media_type="image/png")


@app.get("/compare")
def compare(tokens: str = Query(..., description="Adresses séparées par des virgules"),
            chain: str = "ethereum",
            detect: bool = True):
    chain = _check("0x" + "0" * 40, chain)  # valide seulement la chaîne
    addresses = list(dict.fromkeys(t.strip() for t in tokens.split(",") if t.strip()))
    if not addresses:
        raise HTTPException(400, "Fournis au moins une adresse dans `tokens`.")
    if len(addresses) > MAX_COMPARE:
        raise HTTPException(400, f"Maximum {MAX_COMPARE} tokens par comparaison.")

    results = []
    for addr in addresses:
        # Une erreur sur un token ne doit pas faire échouer toute la comparaison.
        if not ADDRESS_RE.fullmatch(addr):
            results.append({"token_address": addr, "error": "Adresse invalide"})
            continue
        try:
            data, split, balances = _load(addr, chain, detect)
            s = _summary(data, split, balances)
            results.append({k: s[k] for k in ("token_address", "symbol", "total_holders", "gini",
                                              "top10_share", "top50_share", "nakamoto_coefficient",
                                              "risk_score")})
        except (collector.EtherscanError, ValueError) as e:
            results.append({"token_address": addr.lower(), "error": str(e)})
    return {"chain": chain, "tokens": results}
