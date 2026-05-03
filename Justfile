test:
    PYTHONPATH=. .venv/bin/pytest tests/ -v

test-fast:
    PYTHONPATH=. .venv/bin/pytest tests/ -q

install:
    uv pip install -e ".[dev]"
    sed -i '' 's|^import sys$|import sys\nsys.path.insert(0, "{{justfile_directory()}}")|' \
        .venv/bin/llmc
