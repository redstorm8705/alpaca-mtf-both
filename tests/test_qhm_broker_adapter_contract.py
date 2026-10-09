#!/usr/bin/env python3
# ruff: noqa: E501
"""The QHM broker adapter in main.py (_QHMBroker) must accept every keyword the QHM order dispatcher passes to it.

2026-10-09: the dispatcher began passing client_order_id (#442) but the adapter did not accept it, so every QHM buy
raised TypeError (NVDA tranche 1 failed 49 times). Static check on both files, so it runs without importing main.py."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _is_broker(node) -> bool:
    """`broker` or `self.broker` — both reach the _QHMBroker adapter."""
    return ((isinstance(node, ast.Name) and node.id == "broker")
            or (isinstance(node, ast.Attribute) and node.attr == "broker"
                and isinstance(node.value, ast.Name) and node.value.id == "self"))


def _dispatcher_kwargs() -> dict:
    """{broker method: keywords} for every `broker.<method>(...)` / `self.broker.<method>(...)` call in
    quarterly_hold_manager.py."""
    tree = ast.parse((ROOT / "execution" / "quarterly_hold_manager.py").read_text(encoding="utf-8"))
    out: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and _is_broker(n.func.value):
            out.setdefault(n.func.attr, set()).update(k.arg for k in n.keywords if k.arg)
    return out


def _broker_params(name: str) -> set:
    """Parameter names of execution/broker.py's module-level function `name`."""
    tree = ast.parse((ROOT / "execution" / "broker.py").read_text(encoding="utf-8"))
    f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return {a.arg for a in f.args.args + f.args.kwonlyargs}


def _adapter_methods() -> dict:
    """{method: (param names, keywords forwarded)} for class _QHMBroker in main.py."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "_QHMBroker")
    out = {}
    for f in cls.body:
        if isinstance(f, ast.FunctionDef):
            params = {a.arg for a in f.args.args + f.args.kwonlyargs}
            fwd = {k.arg for c in ast.walk(f) if isinstance(c, ast.Call) for k in c.keywords if k.arg}
            out[f.name] = (params, fwd)
    return out


class QHMAdapterContract(unittest.TestCase):
    def test_adapter_accepts_and_forwards_every_dispatcher_keyword(self):
        adapter = _adapter_methods()
        calls = _dispatcher_kwargs()
        self.assertIn("submit_limit_order", calls)
        for method, kws in calls.items():
            self.assertIn(method, adapter, f"_QHMBroker has no {method}")
            params, fwd = adapter[method]
            for kw in kws:
                self.assertIn(kw, params, f"_QHMBroker.{method} does not accept {kw}=")
                self.assertIn(kw, fwd, f"_QHMBroker.{method} does not forward {kw}=")

    def test_forwarded_keywords_exist_on_the_broker_functions(self):
        # The adapter forwards to the same-named function in execution/broker.py (get_position -> get_open_position).
        names = {"get_position": "get_open_position"}
        for method, (_params, fwd) in _adapter_methods().items():
            target = names.get(method, method)
            for kw in fwd:
                self.assertIn(kw, _broker_params(target), f"broker.{target} has no parameter {kw}")


if __name__ == "__main__":
    unittest.main()
