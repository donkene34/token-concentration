# token-concentration

Analyse la distribution des holders d'un token ERC-20 : tu donnes une adresse de contrat, l'outil récupère les holders et leurs soldes, puis calcule des métriques standard de concentration (indice de Gini, coefficient de Nakamoto, part des plus gros holders), une courbe de Lorenz et un score de risque. Le tout est exposé par une API FastAPI.

## Méthodologie

Les métriques s'appliquent aux **holders retenus** : tous les holders moins les adresses exclues (voir plus bas). Les parts sont donc des parts de l'offre en circulation, pas de la `total_supply` brute. Les métriques ne dépendent pas de l'échelle (soldes bruts ou divisés par `10**decimals`, même résultat).

| Métrique | Calcul |
|---|---|
| **Gini** | Soldes triés par ordre croissant `x_1 ≤ … ≤ x_n` : `G = 2·Σ(i·x_i) / (n·Σx) − (n+1)/n`. Équivaut à `1 − 2 × (aire sous la courbe de Lorenz)`. 0 = égalité parfaite, proche de 1 = un seul holder détient presque tout. |
| **Courbe de Lorenz** | Points `(% cumulé des holders, % cumulé de l'offre)`, des plus petits aux plus gros holders. La diagonale représente l'égalité parfaite ; l'aire entre la diagonale et la courbe vaut `Gini / 2`. |
| **Top N share** | Somme des N plus gros soldes / somme de tous les soldes retenus (N = 10, 50, 100). |
| **Nakamoto** | Plus petit nombre de holders dont la somme atteint 51 % de l'offre retenue. Plus il est bas, plus le contrôle est concentré. |

### Risk score

Basé sur le Gini et la part du top 10 (seuils dans `metrics.py`) :

| Niveau | Condition |
|---|---|
| **Élevé** | top 10 ≥ 60 % **ou** Gini ≥ 0,99 |
| **Faible** | top 10 < 25 % **et** Gini < 0,95 |
| **Modéré** | tout le reste |

Le Gini d'une liste de holders est presque toujours très haut (souvent > 0,95), parce qu'il y a beaucoup d'adresses quasi vides. Ses seuils sont donc serrés en haut de l'échelle, et c'est le top 10 qui discrimine le plus. Ce sont des **repères heuristiques, pas une norme** : utilise `/compare` pour situer un token par rapport à d'autres.

## Pourquoi exclure certains contrats

Sans exclusion, un pool de liquidité Uniswap apparaît comme « le plus gros holder » : il détient beaucoup de tokens, mais ce n'est pas un investisseur. Même chose pour l'adresse burn (`0x…dead`), un contrat de staking ou la trésorerie du projet. Les compter gonfle artificiellement la concentration, ou la fausse dans un sens ou dans l'autre, et fausse donc le Gini et le Nakamoto.

L'exclusion (`known_contracts.py`) suit cet ordre de priorité :

1. **Ajout manuel** pour un token donné (`manual_exclusions.json`).
2. **Liste fixe** : adresses burn et routers/PoolManager Uniswap (Ethereum).
3. **Détection auto d'un pool AMM** (appels `token0()` / `token1()`, style Uniswap V2/V3/Sushi) sur les 50 plus gros holders.
4. **Contrat non identifié** : il reste **dans** les calculs mais est signalé dans `contracts_to_review`, pour que tu décides de l'exclure ou non.

Pour exclure une adresse à la main (catégories : `burn`, `dex`, `staking`, `treasury`, `other`) :

```powershell
python known_contracts.py add 0xTOKEN 0xADRESSE treasury --label "Trésorerie"
python known_contracts.py classify 0xTOKEN
```

## Données

`collector.py` interroge l'API **Etherscan V2** (une seule clé pour toutes les chaînes) :

- **Plan API Pro** : endpoint `tokenholderlist`, réponse directe et rapide.
- **Clé gratuite** : repli automatique sur la reconstruction des soldes à partir des events `Transfer` (`getLogs`). Ça marche mais c'est **lent** : prévoir quelques minutes pour un petit token, et c'est impraticable pour un gros token (USDC, par exemple).

Chaînes disponibles : `ethereum`, `base`, `arbitrum`, `optimism`, `polygon`, `bsc` (dictionnaire `CHAINS` dans `collector.py`, une ligne par chaîne à ajouter). Les résultats sont mis en cache dans `data/` (JSON, 24 h).

## Installation

Python 3.11 ou plus récent (testé avec 3.13).

```powershell
pip install -r requirements.txt
$env:ETHERSCAN_API_KEY="ta_cle"      # à redéfinir dans chaque nouvelle fenêtre PowerShell
```

Sous Linux/macOS : `export ETHERSCAN_API_KEY=ta_cle`.

## Utilisation

### En ligne de commande

```powershell
python collector.py 0xTOKEN --chain ethereum        # collecte + cache
python known_contracts.py classify 0xTOKEN          # retenus / exclus / à vérifier
python metrics.py 0xTOKEN                           # Gini, top N, Nakamoto, risque
python visualize.py 0xTOKEN                         # PNG Lorenz + top 20 dans data/
python test_metrics.py                              # tests unitaires
```

### API

```powershell
uvicorn main:app --reload
```

- **Interface web** : http://127.0.0.1:8000/ (une page HTML unique, servie par la même app : analyse d'un token et comparaison de plusieurs tokens)
- **Documentation interactive de l'API** : http://127.0.0.1:8000/docs

| Endpoint | Rôle |
|---|---|
| `GET /analyze/{token_address}?chain=ethereum` | Collecte + calculs, retourne les métriques en JSON |
| `GET /analyze/{token_address}/lorenz` | Image PNG de la courbe de Lorenz |
| `GET /analyze/{token_address}/top` | Image PNG des 20 plus gros holders |
| `GET /compare?tokens=addr1,addr2,addr3` | Mêmes métriques côte à côte (10 tokens max) |

Paramètre optionnel `detect=false` : saute les appels réseau de détection (pools/contrats), plus rapide. Les parts sont des fractions de 0 à 1.

Exemple (sous PowerShell, utilise `curl.exe` : `curl` y est un alias d'une autre commande) :

```powershell
curl.exe "http://127.0.0.1:8000/analyze/0x6085EF8f41c6A1006C6689620546B46F0d82f03c?detect=false"
```

Résultat attendu pour ce token (DRL, un wallet y détient 99,99 % de l'offre ; les valeurs exactes évoluent avec la chaîne) :

```json
{
  "token_address": "0x6085ef8f41c6a1006c6689620546b46f0d82f03c",
  "chain": "ethereum",
  "symbol": "DRL",
  "total_holders": 45,
  "holders_raw": 46,
  "gini": 0.9778,
  "top10_share": 1.0,
  "top50_share": 1.0,
  "top100_share": 1.0,
  "nakamoto_coefficient": 1,
  "risk_score": "Élevé",
  "excluded_contracts": [
    {"address": "0x000000000004444c5dc75cb358380d2e3de08a90", "category": "dex",
     "label": "Uniswap V4 PoolManager", "source": "static", "share_of_supply": 0.0}
  ],
  "contracts_to_review": []
}
```

Codes d'erreur : `400` adresse ou chaîne invalide, `422` aucun holder retenu, `502` erreur Etherscan.

## Structure

```
token-concentration/
├── collector.py         # holders, supply, decimals (Etherscan V2) + cache
├── known_contracts.py   # exclusion : burn, DEX, staking, treasury, manuel
├── metrics.py           # Gini, Lorenz, top N, Nakamoto, risk score
├── visualize.py         # PNG : courbe de Lorenz, top 20
├── main.py              # API FastAPI (+ sert la page web)
├── frontend/
│   └── index.html       # interface web (HTML/CSS/JS, sans build)
├── test_metrics.py      # tests unitaires des métriques
├── manual_exclusions.json   # créé à la première exclusion manuelle
├── data/                # cache et PNG générés
├── requirements.txt
└── README.md
```

## Limites

- **Données on-chain publiques uniquement.** L'outil ne sait pas quelle adresse appartient à qui.
- **Wallets liés (sybil) non détectés.** Une même entité qui répartit ses tokens sur 50 adresses paraît comme 50 holders distincts : la concentration réelle est sous-estimée.
- **Wallets d'exchanges centralisés non détectés.** Un wallet de dépôt/retrait de CEX est un wallet classique (pas un contrat), donc il est compté comme un holder. Ajoute-le à la main si tu le connais.
- **Détection de pools limitée** aux pools de type Uniswap V2/V3/Sushi. Balancer, Curve, etc. finissent dans `contracts_to_review` ou passent inaperçus s'ils sont hors du top 50 analysé.
- **Gini plafonné à `(n−1)/n`** : avec 45 holders, il ne peut pas dépasser 0,978, donc le seuil « Gini ≥ 0,99 » ne joue qu'à partir de 100 holders. Sur un petit token, c'est le top 10 qui décide du risque.
- **Reconstruction par les logs** : lente avec une clé gratuite. Les tokens à rebase ou à taxe de transfert peuvent avoir des soldes qui ne se déduisent pas des seuls events `Transfer` (un avertissement est loggé si la somme s'écarte de plus de 1 % de la `total_supply`).
- **Seuils du risk score heuristiques**, à recalibrer.
- **Instantané** : l'état est celui de la dernière collecte (cache de 24 h), sans historique.
