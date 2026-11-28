"""Opzioni avanzate (lotto 2): il clustering esatto non cambia; l'approssimato con topk >= n coincide con l'esatto."""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan.faces import _blockwise_components  # noqa: E402


def _brute(mat, thr):
    n = len(mat)
    parent = list(range(n))

    def f(x):
        while parent[x] != x:
            x = parent[x]
        return x
    for i in range(n):
        for j in range(i + 1, n):
            if float(mat[i] @ mat[j]) >= thr:
                parent[f(j)] = f(i)
    groups = {}
    for i in range(n):
        groups.setdefault(f(i), []).append(i)
    return sorted(sorted(g) for g in groups.values())


def _norm(comps):
    return sorted(sorted(c) for c in comps)


def _data(seed=0, n=300, d=16):
    rng = np.random.default_rng(seed)
    base = rng.normal(size=(20, d))
    x = np.concatenate([base[i % 20] + 0.05 * rng.normal(size=d) for i in range(n)]).reshape(n, d)
    return (x / np.linalg.norm(x, axis=1, keepdims=True)).astype(np.float32)


def test_exact_matches_brute_force_small_blocks():
    mat = _data()
    assert _norm(_blockwise_components(mat, 0.9, block=64)) == _brute(mat, 0.9)


def test_approx_with_topk_covering_all_neighbours_equals_exact():
    mat = _data()
    exact = _norm(_blockwise_components(mat, 0.9, block=64))
    approx = _norm(_blockwise_components(mat, 0.9, block=64, topk=len(mat)))
    assert approx == exact


def test_approx_small_topk_never_merges_more_than_exact():
    mat = _data()
    exact = _norm(_blockwise_components(mat, 0.9, block=64))
    approx = _norm(_blockwise_components(mat, 0.9, block=64, topk=2))
    # ogni componente approssimata e' contenuta in una esatta: puo' solo separare, mai unire di piu'
    exact_of = {i: k for k, c in enumerate(exact) for i in c}
    for c in approx:
        assert len({exact_of[i] for i in c}) == 1


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} test superati")
