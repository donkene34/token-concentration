"""Tests unitaires de metrics.py. Lance : python test_metrics.py  (ou pytest)."""
import numpy as np

import metrics as m


def test_equal_distribution():
    b = [5] * 10
    assert abs(m.gini_coefficient(b)) < 1e-12
    x, y = m.lorenz_curve(b)
    assert np.allclose(x, y)                      # la courbe suit la diagonale
    assert abs(m.top_n_share(b, 10) - 1.0) < 1e-12
    assert abs(m.top_n_share(b, 5) - 0.5) < 1e-12


def test_single_holder():
    assert m.gini_coefficient([42]) == 0.0        # population d'un seul : 0 par la formule
    assert m.top_n_share([42], 10) == 1.0
    assert m.nakamoto_coefficient([42]) == 1
    x, y = m.lorenz_curve([42])
    assert list(x) == [0.0, 1.0] and list(y) == [0.0, 1.0]
    assert m.risk_score(0.0, 1.0) == "Élevé"      # sauvé par le top10


def test_gini_known_value():
    assert abs(m.gini_coefficient([1, 2, 3, 4]) - 0.25) < 1e-12


def test_one_whale_many_dust():
    b = [1e12] + [1] * 999
    assert m.gini_coefficient(b) > 0.99
    assert m.nakamoto_coefficient(b) == 1


def test_gini_equals_one_minus_twice_lorenz_area():
    rng = np.random.default_rng(0)
    b = rng.pareto(1.5, 500) + 1
    x, y = m.lorenz_curve(b)
    area = np.sum((y[1:] + y[:-1]) / 2 * np.diff(x))   # trapèzes
    assert abs(m.gini_coefficient(b) - (1 - 2 * area)) < 1e-10


def test_scale_invariance_and_big_ints():
    b = [1, 2, 3, 4, 10]
    assert abs(m.gini_coefficient(b) - m.gini_coefficient([v * 10**18 for v in b])) < 1e-12


def test_top_n_share():
    b = [50, 30, 10, 5, 5]
    assert abs(m.top_n_share(b, 2) - 0.80) < 1e-12
    assert m.top_n_share(b, 100) == 1.0


def test_nakamoto():
    b = [50, 30, 10, 5, 5]
    assert m.nakamoto_coefficient(b) == 2                    # 80 % >= 51 %
    assert m.nakamoto_coefficient(b, 0.9) == 3               # 90 % >= 90 %
    assert m.nakamoto_coefficient([51, 49]) == 1             # pile 51 % : pas d'erreur d'arrondi
    assert m.nakamoto_coefficient([1] * 100) == 51


def test_risk_score():
    assert m.risk_score(0.80, 0.20) == "Faible"
    assert m.risk_score(0.97, 0.20) == "Modéré"
    assert m.risk_score(0.50, 0.70) == "Élevé"
    assert m.risk_score(0.995, 0.10) == "Élevé"
    assert m.risk_score(0.80, 0.25) == "Modéré"   # 25 % exactement : pas Faible
    assert m.risk_score(0.95, 0.10) == "Modéré"   # Gini 0,95 exactement : pas Faible
    assert m.risk_score(0.80, 0.60) == "Élevé"    # 60 % exactement : Élevé


def test_invalid_inputs():
    for bad in ([], [-1, 2], [0, 0], [1, float("nan")]):
        try:
            m.gini_coefficient(bad)
            raise AssertionError(f"aurait dû échouer : {bad}")
        except ValueError:
            pass
    for call in (lambda: m.top_n_share([1], 0), lambda: m.nakamoto_coefficient([1], 0)):
        try:
            call()
            raise AssertionError("aurait dû échouer")
        except ValueError:
            pass


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"OK  {name}")
    print(f"{len(tests)} tests passés.")
