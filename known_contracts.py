"""known_contracts.py : Phase 2. Sépare les vrais holders des adresses à exclure.

Pourquoi : un pool de liquidité, une adresse burn ou un contrat de staking
apparaissent comme "le plus gros holder" alors qu'ils ne représentent pas un
investisseur. Les compter fausse le Gini et le Nakamoto coefficient.

Ordre de priorité pour classer une adresse :
  1. ajout manuel pour ce token  (manual_exclusions.json)
  2. liste fixe                   (burn, routers/PoolManager Uniswap)
  3. détection auto d'un pool AMM (appels token0()/token1(), style Uniswap V2/V3)
  4. contrat non identifié        -> GARDÉ dans les calculs, mais signalé "à vérifier"

La détection auto ne porte que sur les `top_n_check` plus gros holders : au-delà,
une adresse pèse trop peu pour changer les métriques.

Usage :
    python known_contracts.py classify 0xTOKEN --chain ethereum
    python known_contracts.py add 0xTOKEN 0xADRESSE treasury --label "Trésorerie"
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

import collector
from collector import EtherscanError

log = logging.getLogger("known_contracts")

# Catégories qui sortent des calculs. "other" sert aux cas manuels (vesting, CEX...).
EXCLUDED_CATEGORIES = ("burn", "dex", "staking", "treasury", "other")

MANUAL_FILE = Path(__file__).parent / "manual_exclusions.json"

# Affichage CLI : les contrats non identifiés sous ce seuil (part de l'offre) sont masqués.
REVIEW_MIN_SHARE = 0.001  # 0,1 %

# Valables sur toutes les chaînes.
BURN_ADDRESSES = {
    "0x" + "0" * 40: "Adresse zéro",
    "0x000000000000000000000000000000000000dead": "Adresse dead (burn)",
}

# Adresses fixes propres à une chaîne. Les pools, eux, sont par token :
# ils sont repérés automatiquement (voir _is_pool) ou ajoutés à la main.
KNOWN_BY_CHAIN = {
    "ethereum": {
        "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": ("dex", "Uniswap V2 Router"),
        "0xe592427a0aece92de3edee1f18e0157c05861564": ("dex", "Uniswap V3 SwapRouter"),
        "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": ("dex", "Uniswap V3 SwapRouter02"),
        "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": ("dex", "Uniswap Universal Router"),
        "0x000000000004444c5dc75cb358380d2e3de08a90": ("dex", "Uniswap V4 PoolManager"),
    },
}


# --------------------------------------------------------------------------- #
# Ajouts manuels par token
# --------------------------------------------------------------------------- #
def _load_manual() -> dict:
    if not MANUAL_FILE.exists():
        return {}
    return json.loads(MANUAL_FILE.read_text(encoding="utf-8"))


def add_manual_exclusion(token: str, chain: str, address: str,
                         category: str, label: str = "") -> None:
    """Ajoute (ou met à jour) une adresse à exclure pour un token donné."""
    if category not in EXCLUDED_CATEGORIES:
        raise ValueError(f"Catégorie '{category}' invalide. Choix : {', '.join(EXCLUDED_CATEGORIES)}")
    for value in (token, address):
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
            raise ValueError(f"Adresse invalide : {value}")
    data = _load_manual()
    key = f"{chain.lower()}:{token.lower()}"
    data.setdefault(key, {})[address.lower()] = {"category": category, "label": label}
    MANUAL_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_manual_exclusions(token: str, chain: str) -> dict:
    """{adresse: (catégorie, label)} pour ce token."""
    entries = _load_manual().get(f"{chain.lower()}:{token.lower()}", {})
    return {a: (e["category"], e.get("label", "")) for a, e in entries.items()}


# --------------------------------------------------------------------------- #
# Détection automatique (contrat ? pool ?)
# --------------------------------------------------------------------------- #
def _is_contract(address: str, chain: str) -> bool:
    code = collector._proxy(chain, "eth_getCode", address=address, tag="latest")
    return code not in ("0x", "0x0")


def _returns_address(to: str, chain: str, selector: str) -> bool:
    """True si l'appel renvoie une adresse non nulle (un revert donne False)."""
    try:
        res = collector._proxy(chain, "eth_call", to=to, data=selector, tag="latest")
    except (EtherscanError, KeyError):
        return False
    return isinstance(res, str) and len(res) == 66 and int(res, 16) != 0


def _is_pool(address: str, chain: str) -> bool:
    """Pool AMM style Uniswap V2/V3/Sushi : expose token0() et token1()."""
    return (_returns_address(address, chain, "0x0dfe1681")      # token0()
            and _returns_address(address, chain, "0xd21220a7"))  # token1()


def _addr_cache_path() -> Path:
    return collector.DATA_DIR / "address_classes.json"


def _load_addr_cache() -> dict:
    path = _addr_cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _save_addr_cache(cache: dict) -> None:
    collector.DATA_DIR.mkdir(exist_ok=True)
    _addr_cache_path().write_text(json.dumps(cache), encoding="utf-8")


def _detect(address: str, chain: str, cache: dict) -> dict:
    """{'is_contract': bool, 'is_pool': bool}, mis en cache (ça ne change pas)."""
    key = f"{chain}:{address}"
    if key not in cache:
        contract = _is_contract(address, chain)
        cache[key] = {"is_contract": contract,
                      "is_pool": contract and _is_pool(address, chain)}
    return cache[key]


# --------------------------------------------------------------------------- #
# Fonction principale
# --------------------------------------------------------------------------- #
def classify_holders(token_data: dict, detect: bool = True, top_n_check: int = 50) -> dict:
    """Sépare les holders retournés par collector.get_token_holders.

    Retourne :
      kept        : holders gardés pour les calculs (inclut les contrats "à vérifier")
      excluded    : [{address, balance, category, label, source}]
      to_review   : contrats non identifiés (déjà dans `kept`, juste signalés)
      kept_total / excluded_total : sommes des soldes bruts
    """
    token, chain = token_data["token_address"], token_data["chain"]
    manual = get_manual_exclusions(token, chain)
    static = {a: ("burn", label) for a, label in BURN_ADDRESSES.items()}
    static.update(KNOWN_BY_CHAIN.get(chain, {}))
    cache = _load_addr_cache() if detect else {}

    kept, excluded, to_review = [], [], []
    for rank, holder in enumerate(token_data["holders"]):
        addr = holder["address"]
        if addr in manual:
            category, label = manual[addr]
            excluded.append({**holder, "category": category, "label": label, "source": "manual"})
            continue
        if addr in static:
            category, label = static[addr]
            excluded.append({**holder, "category": category, "label": label, "source": "static"})
            continue
        if detect and rank < top_n_check:
            info = _detect(addr, chain, cache)
            if info["is_pool"]:
                excluded.append({**holder, "category": "dex",
                                 "label": "Pool AMM (token0/token1)", "source": "auto"})
                continue
            if info["is_contract"]:
                to_review.append({**holder, "label": "Contrat non identifié"})
        kept.append(holder)

    if detect:
        _save_addr_cache(cache)
    return {
        "kept": kept,
        "excluded": excluded,
        "to_review": to_review,
        "kept_total": sum(h["balance"] for h in kept),
        "excluded_total": sum(h["balance"] for h in excluded),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_report(token_data: dict, result: dict) -> None:
    scale = 10 ** token_data["decimals"]
    supply = token_data["total_supply"]
    pct = lambda v: f"{v / supply:.2%}"
    print(f"{token_data['symbol']} ({token_data['chain']}) | {len(token_data['holders'])} holders bruts")
    print(f"Retenus : {len(result['kept'])} ({pct(result['kept_total'])} de l'offre) | "
          f"Exclus : {len(result['excluded'])} ({pct(result['excluded_total'])})")
    for e in result["excluded"]:
        print(f"  EXCLU [{e['category']}/{e['source']}] {e['address']}  "
              f"{e['balance'] / scale:>18,.2f} ({pct(e['balance'])})  {e['label']}")
    shown = [r for r in result["to_review"] if r["balance"] / supply >= REVIEW_MIN_SHARE]
    for r in shown:
        print(f"  A VERIFIER {r['address']}  {r['balance'] / scale:>18,.2f} "
              f"({pct(r['balance'])})  {r['label']}")
    hidden = len(result["to_review"]) - len(shown)
    if hidden:
        print(f"  ({hidden} contrat(s) non identifié(s) sous {REVIEW_MIN_SHARE:.1%} de l'offre, "
              f"masqués ; ils restent dans `to_review`)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Classe les holders d'un token.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cls = sub.add_parser("classify", help="Affiche retenus / exclus / à vérifier")
    p_cls.add_argument("token")
    p_cls.add_argument("--chain", default="ethereum", choices=list(collector.CHAINS))
    p_cls.add_argument("--no-detect", action="store_true", help="Sans appels réseau")
    p_cls.add_argument("--top", type=int, default=50, help="Nb de top holders analysés")

    p_add = sub.add_parser("add", help="Exclure une adresse pour un token")
    p_add.add_argument("token")
    p_add.add_argument("address")
    p_add.add_argument("category", choices=EXCLUDED_CATEGORIES)
    p_add.add_argument("--label", default="")
    p_add.add_argument("--chain", default="ethereum", choices=list(collector.CHAINS))

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.cmd == "add":
            add_manual_exclusion(args.token, args.chain, args.address, args.category, args.label)
            print(f"Ajouté : {args.address.lower()} -> {args.category}")
        else:
            data = collector.get_token_holders(args.token, args.chain)
            res = classify_holders(data, detect=not args.no_detect, top_n_check=args.top)
            _print_report(data, res)
    except (EtherscanError, ValueError) as e:
        sys.exit(f"Erreur : {e}")
