"""collector.py : Phase 1. Récupère les holders d'un token ERC-20 via Etherscan V2.

Stratégie :
  1. Endpoint `tokenholderlist` (plan API Pro) : une réponse directe, rapide.
  2. Si indisponible (clé gratuite), reconstruction des soldes à partir des
     events Transfer (`getLogs`). Fonctionne, mais lent sur les gros tokens.

Limites de la reconstruction : un token à rebase ou à taxe de transfert peut
avoir des soldes qui ne se déduisent pas des seuls events Transfer. Un
avertissement est loggé si la somme des soldes s'écarte du total_supply.

Usage rapide :
    set ETHERSCAN_API_KEY=ta_cle        (Windows cmd)
    python collector.py 0xADRESSE_DU_TOKEN --chain ethereum
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("collector")

API_URL = "https://api.etherscan.io/v2/api"

# Nom de chaîne -> chainid Etherscan V2. Ajoute une ligne pour supporter une chaîne.
CHAINS = {
    "ethereum": 1,
    "base": 8453,
    "arbitrum": 42161,
    "optimism": 10,
    "polygon": 137,
    "bsc": 56,
}

DATA_DIR = Path(__file__).parent / "data"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x" + "0" * 40

HOLDER_PAGE_SIZE = 1000   # taille de page de tokenholderlist
LOG_PAGE_SIZE = 1000      # getLogs : page * offset <= 10 000 résultats par requête
LOG_MAX_PAGES = 10
INITIAL_BLOCK_WINDOW = 100_000  # fenêtre de départ, divisée par 2 si elle sature
MIN_DELAY = 0.35          # secondes entre deux appels (clé gratuite : ~3 appels/s)

_last_call = 0.0


class EtherscanError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Appels HTTP
# --------------------------------------------------------------------------- #
def _call(chain: str, params: dict, retries: int = 5) -> dict:
    """Appel Etherscan V2 avec throttling et retry sur rate limit."""
    global _last_call
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        raise EtherscanError("Variable d'environnement ETHERSCAN_API_KEY manquante.")
    full = {**params, "chainid": CHAINS[chain], "apikey": key}

    for attempt in range(retries):
        wait = MIN_DELAY - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()

        try:
            resp = requests.get(API_URL, params=full, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            log.warning("Erreur réseau (%s), nouvel essai %d/%d", type(exc).__name__,
                        attempt + 1, retries)
            time.sleep(2 * (attempt + 1))
            continue
        result = data.get("result")
        if isinstance(result, str) and "rate limit" in result.lower():
            time.sleep(1.5 * (attempt + 1))
            continue
        return data
    raise EtherscanError("Rate limit ou réseau Etherscan instable après plusieurs essais.")


def _proxy(chain: str, action: str, **extra) -> str:
    """Appel JSON-RPC proxy (eth_call, eth_blockNumber...). Retourne `result`."""
    data = _call(chain, {"module": "proxy", "action": action, **extra})
    if "error" in data:
        raise EtherscanError(f"{action}: {data['error']}")
    return data["result"]


# --------------------------------------------------------------------------- #
# Infos du token (total_supply, decimals, symbol)
# --------------------------------------------------------------------------- #
def _decode_symbol(raw: str) -> str:
    """Décode le retour de symbol() : string dynamique, ou bytes32 (ex: MKR)."""
    b = bytes.fromhex(raw[2:])
    if len(b) == 32:
        return b.rstrip(b"\x00").decode("utf-8", errors="replace")
    if len(b) >= 64:
        length = int.from_bytes(b[32:64], "big")
        return b[64:64 + length].decode("utf-8", errors="replace")
    return "?"


def get_token_info(token: str, chain: str = "ethereum") -> dict:
    """total_supply (entier brut), decimals, symbol via appels ERC-20 standard."""
    def eth_call(selector: str) -> str:
        return _proxy(chain, "eth_call", to=token, data=selector, tag="latest")

    total_supply = int(eth_call("0x18160ddd"), 16)   # totalSupply()
    decimals = int(eth_call("0x313ce567"), 16)       # decimals()
    try:
        symbol = _decode_symbol(eth_call("0x95d89b41"))  # symbol()
    except Exception:
        symbol = "?"
    return {"total_supply": total_supply, "decimals": decimals, "symbol": symbol}


# --------------------------------------------------------------------------- #
# Source 1 : tokenholderlist (API Pro)
# --------------------------------------------------------------------------- #
def _holders_from_api(token: str, chain: str) -> dict[str, int]:
    balances: dict[str, int] = {}
    page = 1
    while True:
        data = _call(chain, {
            "module": "token", "action": "tokenholderlist",
            "contractaddress": token, "page": page, "offset": HOLDER_PAGE_SIZE,
        })
        result = data.get("result")
        if not isinstance(result, list):
            # Typiquement : "Sorry, it looks like you are trying to access an API Pro endpoint"
            raise EtherscanError(f"tokenholderlist indisponible : {result}")
        for row in result:
            qty = int(row["TokenHolderQuantity"])
            if qty > 0:
                balances[row["TokenHolderAddress"].lower()] = qty
        if len(result) < HOLDER_PAGE_SIZE:
            return balances
        page += 1


# --------------------------------------------------------------------------- #
# Source 2 : reconstruction depuis les events Transfer
# --------------------------------------------------------------------------- #
def _creation_block(token: str, chain: str) -> int:
    data = _call(chain, {"module": "contract", "action": "getcontractcreation",
                         "contractaddresses": token})
    result = data.get("result")
    if not isinstance(result, list) or not result:
        log.warning("Bloc de création introuvable, scan depuis le bloc 0.")
        return 0
    row = result[0]
    if row.get("blockNumber"):
        return int(row["blockNumber"])
    tx = _proxy(chain, "eth_getTransactionByHash", txhash=row["txHash"])
    return int(tx["blockNumber"], 16)


def _apply_logs(logs: list, balances: dict[str, int]) -> None:
    for entry in logs:
        topics = entry["topics"]
        if len(topics) != 3:  # ignore les Transfer ERC-721 (4 topics)
            continue
        sender = "0x" + topics[1][-40:]
        receiver = "0x" + topics[2][-40:]
        raw = entry["data"]
        value = int(raw, 16) if raw and raw != "0x" else 0
        balances[sender] = balances.get(sender, 0) - value
        balances[receiver] = balances.get(receiver, 0) + value


def _scan(token: str, chain: str, start: int, end: int, balances: dict[str, int]) -> None:
    """Scanne [start, end]. Si la fenêtre sature (10 000 logs), on la coupe en deux."""
    logs: list = []
    saturated = True
    for page in range(1, LOG_MAX_PAGES + 1):
        data = _call(chain, {
            "module": "logs", "action": "getLogs",
            "fromBlock": start, "toBlock": end,
            "address": token, "topic0": TRANSFER_TOPIC,
            "page": page, "offset": LOG_PAGE_SIZE,
        })
        result = data.get("result")
        if not isinstance(result, list):
            raise EtherscanError(f"getLogs a échoué ({start}-{end}) : {result}")
        logs.extend(result)
        if len(result) < LOG_PAGE_SIZE:
            saturated = False
            break

    if saturated:
        if start == end:
            raise EtherscanError(f"Plus de 10 000 transferts dans le seul bloc {start}.")
        mid = (start + end) // 2
        _scan(token, chain, start, mid, balances)
        _scan(token, chain, mid + 1, end, balances)
        return

    _apply_logs(logs, balances)
    log.info("blocs %d-%d : %d transferts", start, end, len(logs))


def _holders_from_logs(token: str, chain: str) -> dict[str, int]:
    start = _creation_block(token, chain)
    latest = int(_proxy(chain, "eth_blockNumber"), 16)
    log.info("Reconstruction depuis les logs : blocs %d -> %d", start, latest)

    balances: dict[str, int] = {}
    cursor = start
    while cursor <= latest:
        end = min(cursor + INITIAL_BLOCK_WINDOW - 1, latest)
        _scan(token, chain, cursor, end, balances)
        cursor = end + 1

    balances.pop(ZERO_ADDRESS, None)  # compte mint/burn, pas un vrai holder
    return {a: b for a, b in balances.items() if b > 0}


# --------------------------------------------------------------------------- #
# Cache JSON (un fichier par chaîne + token)
# --------------------------------------------------------------------------- #
def _cache_path(token: str, chain: str) -> Path:
    return DATA_DIR / f"{chain}_{token}.json"


def _load_cache(token: str, chain: str, max_age_hours: float) -> dict | None:
    path = _cache_path(token, chain)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(cached["fetched_at"])
    except (ValueError, KeyError):
        return None  # cache corrompu : on le recalcule
    if datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_hours):
        return None
    cached["total_supply"] = int(cached["total_supply"])
    for h in cached["holders"]:
        h["balance"] = int(h["balance"])
    return cached


def _save_cache(result: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    # Les entiers on-chain dépassent 2^53 : on les stocke en chaînes dans le JSON.
    out = {
        **result,
        "total_supply": str(result["total_supply"]),
        "holders": [{"address": h["address"], "balance": str(h["balance"])}
                    for h in result["holders"]],
    }
    _cache_path(result["token_address"], result["chain"]).write_text(
        json.dumps(out), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Fonction principale
# --------------------------------------------------------------------------- #
def get_token_holders(token_address: str, chain: str = "ethereum",
                      use_cache: bool = True, max_age_hours: float = 24) -> dict:
    """Retourne les holders d'un token ERC-20.

    {
      token_address, chain, symbol, decimals, total_supply (entier brut),
      source ("etherscan_tokenholderlist" | "transfer_logs"), fetched_at,
      holders: [{address, balance (entier brut)}]   # triés par solde décroissant
    }
    Les soldes sont des entiers bruts : divise par 10**decimals pour l'affichage.
    """
    chain = chain.lower()
    if chain not in CHAINS:
        raise ValueError(f"Chaîne inconnue '{chain}'. Disponibles : {', '.join(CHAINS)}")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", token_address):
        raise ValueError(f"Adresse invalide : {token_address}")
    token = token_address.lower()

    if use_cache:
        cached = _load_cache(token, chain, max_age_hours)
        if cached:
            log.info("Cache utilisé : %s", _cache_path(token, chain).name)
            return cached

    info = get_token_info(token, chain)
    try:
        balances = _holders_from_api(token, chain)
        source = "etherscan_tokenholderlist"
    except EtherscanError as exc:
        log.warning("%s -> repli sur la reconstruction via les logs.", exc)
        balances = _holders_from_logs(token, chain)
        source = "transfer_logs"

    held = sum(balances.values())
    if info["total_supply"] and abs(held - info["total_supply"]) / info["total_supply"] > 0.01:
        log.warning("Somme des soldes (%d) != total_supply (%d) à plus de 1%% : "
                    "token à rebase/taxe, burn hors Transfer, ou données incomplètes.",
                    held, info["total_supply"])

    result = {
        "token_address": token,
        "chain": chain,
        **info,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "holders": [{"address": a, "balance": b}
                    for a, b in sorted(balances.items(), key=lambda kv: kv[1], reverse=True)],
    }
    _save_cache(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test rapide du collecteur de holders.")
    parser.add_argument("token", help="Adresse du contrat ERC-20")
    parser.add_argument("--chain", default="ethereum", choices=list(CHAINS))
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        r = get_token_holders(args.token, args.chain, use_cache=not args.no_cache)
    except (EtherscanError, ValueError, requests.RequestException) as e:
        sys.exit(f"Erreur : {e}")

    scale = 10 ** r["decimals"]
    print(f"{r['symbol']} ({r['chain']}) | source: {r['source']}")
    print(f"total_supply: {r['total_supply'] / scale:,.2f} | holders: {len(r['holders'])}")
    for h in r["holders"][:5]:
        print(f"  {h['address']}  {h['balance'] / scale:>20,.2f}  "
              f"({h['balance'] / r['total_supply']:.2%})")
