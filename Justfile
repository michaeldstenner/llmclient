test:
    PYTHONPATH=. .venv/bin/pytest tests/ -v

test-fast:
    PYTHONPATH=. .venv/bin/pytest tests/ -q

install:
    uv pip install -e ".[dev]"
