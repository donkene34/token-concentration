"""metrics.py : Phase 3. Métriques de concentration sur une liste de soldes.

Toutes les fonctions prennent une liste/array de soldes (un par holder) et
calculent les parts par rapport à la SOMME des soldes fournis. Passe donc les
holders RETENUS (après exclusion des pools, burn...) : les parts sont alors
exprimées en % de l'offre en circulation, pas de la total_supply brute.

Les métriques ne dépendent pas de l'échelle : soldes bruts ou divisés par
10**decimals donnent le même résultat.

Usage rapide sur un token déjà collecté :
    python metrics.py 0xTOKEN --chain ethereum
"""
import argparse
import sys

import numpy as np

# --- Seuils du risk_score (validés avec Donkene) ---------------------------- #
# Le Gini calculé sur une liste de holders est presque toujours très haut
# (souvent > 0,95) car il y a énormément d'adresses quasi vides. Ses seuils sont
# donc serrés en haut de l'échelle, et c'est le top 10 qui discrimine le plus.
# Ce sont des repères heuristiques, pas une norme : à recalibrer avec /compare.
TOP10_HIGH = 0.60   # 10 adresses > 60 % de l'offre : une poignée pèse plus que tout le reste
GINI_HIGH = 0.99    # quasi-monopole : quasi toute la richesse dans très peu d'adresses
TOP10_LOW = 0.25    # sous 25 % dans le top 10 : distribution réellement répartie
GINI_LOW = 0.95     # et sans la longue traîne extrême typique d'un token très concentré


def _clean(balances) -> np.ndarray:
    """Convertit en array float64 et valide (non vide, sans négatif, total > 0)."""
    arr = np.asarray(balances, dtype=float)
    if arr.ndim != 1 or arr.size == 0:
        raise ValueError("balances doit être une liste non vide de soldes.")
    if np.isnan(arr).any() or (arr < 0).any():
        raise ValueError("balances ne doit contenir ni NaN ni solde négatif.")
    if arr.sum() <= 0:
        raise ValueError("La somme des soldes doit être positive.")
    return arr


def gini_coefficient(balances) -> float:
    """Indice de Gini : 0 = égalité parfaite, proche de 1 = un seul détient tout.

    Formule sur soldes triés par ordre croissant x_1 <= ... <= x_n :
        G = 2 * sum(i * x_i) / (n * sum(x)) - (n + 1) / n
    Elle est égale à 1 - 2 * (aire sous la courbe de Lorenz).
    Avec un seul holder (n = 1), la formule donne 0 : il n'y a pas d'inégalité
    mesurable dans une population d'un seul. Le risk_score le classera Élevé
    via le top10 (100 %).
    """
    x = np.sort(_clean(balances))
    n = x.size
    i = np.arange(1, n + 1)
    return float(2 * np.sum(i * x) / (n * x.sum()) - (n + 1) / n)


def lorenz_curve(balances) -> tuple[np.ndarray, np.ndarray]:
    """Points de la courbe de Lorenz.

    Retourne (x, y) de longueur n + 1, avec x = % cumulé de la population
    (des plus petits holders aux plus gros) et y = % cumulé de la richesse,
    en fractions de 0 à 1. Part toujours de (0, 0) et finit à (1, 1).
    """
    s = np.sort(_clean(balances))
    x = np.arange(s.size + 1) / s.size
    y = np.concatenate(([0.0], np.cumsum(s) / s.sum()))
    return x, y


def top_n_share(balances, n: int) -> float:
    """Part (0 à 1) détenue par les n plus gros holders. Si n > nb de holders : 1.0."""
    if n <= 0:
        raise ValueError("n doit être >= 1.")
    s = np.sort(_clean(balances))[::-1]
    return float(s[:n].sum() / s.sum())


def nakamoto_coefficient(balances, threshold: float = 0.51) -> int:
    """Nombre minimum de holders dont la somme atteint `threshold` de l'offre.

    On additionne les plus gros soldes jusqu'à atteindre le seuil (51 % par
    défaut, la majorité). Plus il est petit, plus le contrôle est concentré.
    """
    if not 0 < threshold <= 1:
        raise ValueError("threshold doit être dans ]0, 1].")
    s = np.sort(_clean(balances))[::-1]
    cumulative = np.cumsum(s)
    # Petite tolérance pour éviter qu'un arrondi flottant (0.5099999...) fasse rater le seuil.
    target = threshold * s.sum() * (1 - 1e-12)
    return int(np.searchsorted(cumulative, target) + 1)


def risk_score(gini: float, top10_share: float) -> str:
    """Classe la concentration : "Faible", "Modéré" ou "Élevé".

    Élevé  : top10 >= 60 %  OU  Gini >= 0,99
    Faible : top10 <  25 %  ET  Gini <  0,95
    Modéré : tout le reste
    Un seul critère extrême suffit à passer en Élevé ; il faut au contraire
    les deux critères bons pour être Faible. Les seuils sont définis en haut du fichier.
    """
    if top10_share >= TOP10_HIGH or gini >= GINI_HIGH:
        return "Élevé"
    if top10_share < TOP10_LOW and gini < GINI_LOW:
        return "Faible"
    return "Modéré"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Métriques de concentration d'un token.")
    parser.add_argument("token")
    parser.add_argument("--chain", default="ethereum")
    parser.add_argument("--no-detect", action="store_true", help="Sans appels réseau d'exclusion")
    args = parser.parse_args()

    import collector
    import known_contracts

    try:
        data = collector.get_token_holders(args.token, args.chain)
        split = known_contracts.classify_holders(data, detect=not args.no_detect)
    except (collector.EtherscanError, ValueError) as e:
        sys.exit(f"Erreur : {e}")

    scale = 10 ** data["decimals"]
    bal = [h["balance"] / scale for h in split["kept"]]
    if not bal:
        sys.exit("Aucun holder retenu après exclusion.")
    g, t10 = gini_coefficient(bal), top_n_share(bal, 10)
    print(f"{data['symbol']} ({data['chain']}) | {len(bal)} holders retenus, "
          f"{len(split['excluded'])} exclus")
    print(f"Gini            : {g:.4f}")
    for n in (10, 50, 100):
        print(f"Top {n:<3} share   : {top_n_share(bal, n):.2%}")
    print(f"Nakamoto (51 %) : {nakamoto_coefficient(bal)}")
    print(f"Risque          : {risk_score(g, t10)}")
