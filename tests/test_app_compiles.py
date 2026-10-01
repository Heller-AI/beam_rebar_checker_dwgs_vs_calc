"""The Streamlit page must at least compile (it is not imported by the other tests)."""

from pathlib import Path


def test_app_py_compiles():
    path = Path(__file__).resolve().parent.parent / "app.py"
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
