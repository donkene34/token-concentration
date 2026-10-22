"""visualize.py : Phase 4. Graphiques PNG de la concentration d'un token.

  - plot_lorenz        : courbe de Lorenz + ligne d'égalité parfaite
  - plot_top_holders   : barres des N plus gros holders (% de l'offre retenue)

Les parts sont calculées sur les holders retenus (hors pools, burn...), comme
dans metrics.py.

Usage rapide (génère les 2 PNG dans data/) :
    python visualize.py 0xTOKEN --chain ethereum
"""
import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # pas de fenêtre : fonctionne aussi dans FastAPI / sans écran
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import PercentFormatter

import metrics

# Couleurs : une teinte pour la donnée, du gris discret pour tout le reste.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
DATA = "#2a78d6"


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def plot_lorenz(balances, path, title: str = "Courbe de Lorenz") -> Path:
    """Courbe de Lorenz des soldes, avec la diagonale d'égalité parfaite."""
    x, y = metrics.lorenz_curve(balances)
    gini = metrics.gini_coefficient(balances)

    fig, ax = plt.subplots(figsize=(6.4, 6.4), dpi=150, facecolor=SURFACE)
    _style(ax)

    ax.plot([0, 1], [0, 1], color=INK_2, linewidth=1.2, linestyle=(0, (4, 3)))
    ax.fill_between(x, y, x, color=DATA, alpha=0.12, linewidth=0)   # l'aire = le Gini
    ax.plot(x, y, color=DATA, linewidth=2)

    ax.text(0.42, 0.47, "Égalité parfaite", color=INK_2, fontsize=9,
            rotation=45, rotation_mode="anchor", ha="center", va="bottom")
    # Placé dans l'aire ombrée, loin de la courbe, pour ne jamais la recouvrir.
    ax.text(0.28, 0.24, f"Inégalité : {gini * 100:.1f} / 100", color=INK, fontsize=12, fontweight="bold")
    ax.text(0.28, 0.19, "(courbe loin de la ligne = inégal)", color=INK_2, fontsize=8)

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_xlabel("% des détenteurs (du plus petit au plus gros)", color=INK_2, fontsize=9)
    ax.set_ylabel("% des tokens détenus", color=INK_2, fontsize=9)
    ax.set_title(title, color=INK, fontsize=12, loc="left", pad=12)

    return _save(fig, path)


def plot_top_holders(holders, path, n: int = 20, title: str = "Les plus gros détenteurs") -> Path:
    """Barres des n plus gros holders. `holders` : [{address, balance}, ...]
    (les holders retenus). Les parts sont relatives à la somme de tous les `holders`."""
    ranked = sorted(holders, key=lambda h: h["balance"], reverse=True)
    total = sum(h["balance"] for h in ranked)
    if total <= 0:
        raise ValueError("Aucun holder à afficher.")
    top = ranked[:n]
    shares = np.array([h["balance"] / total for h in top])
    labels = [_short(h["address"]) for h in top]

    fig, ax = plt.subplots(figsize=(7.5, 0.3 * len(top) + 1.6), dpi=150, facecolor=SURFACE)
    _style(ax)
    ax.grid(axis="y", visible=False)

    pos = np.arange(len(top))
    ax.barh(pos, shares, height=0.6, color=DATA)
    ax.set_yticks(pos)
    ax.set_yticklabels(labels, family="monospace")
    ax.invert_yaxis()
    ax.set_xlim(0, max(shares.max() * 1.18, 0.01))
    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    for p, s in zip(pos, shares):
        ax.text(s + shares.max() * 0.012, p, f"{s:.2%}", va="center", color=INK_2, fontsize=8)
    ax.set_xlabel("% des tokens détenus (hors adresses écartées)", color=INK_2, fontsize=9)
    ax.set_title(title, color=INK, fontsize=12, loc="left", pad=12)

    return _save(fig, path)


def _save(fig, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Génère les PNG de concentration d'un token.")
    parser.add_argument("token")
    parser.add_argument("--chain", default="ethereum")
    parser.add_argument("--no-detect", action="store_true", help="Sans appels réseau d'exclusion")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    import collector
    import known_contracts

    try:
        data = collector.get_token_holders(args.token, args.chain)
        kept = known_contracts.classify_holders(data, detect=not args.no_detect)["kept"]
    except (collector.EtherscanError, ValueError) as e:
        sys.exit(f"Erreur : {e}")
    if not kept:
        sys.exit("Aucun holder retenu après exclusion.")

    name = f"{data['chain']}_{data['token_address']}"
    label = f"{data['symbol']} ({data['chain']})"
    scale = 10 ** data["decimals"]
    lorenz = plot_lorenz([h["balance"] / scale for h in kept], collector.DATA_DIR / f"{name}_lorenz.png",
                         title=f"Courbe de Lorenz : {label}")
    top = plot_top_holders(kept, collector.DATA_DIR / f"{name}_top.png", n=args.top,
                           title=f"Les {args.top} plus gros détenteurs : {label}")
    print(f"Lorenz : {lorenz}\nTop    : {top}")
