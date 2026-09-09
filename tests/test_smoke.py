import compileall
from pathlib import Path
import pytest
from evatorrent import __version__
from evatorrent.cli import build_parser


def test_compileall_src():
    """Guarantees that every single Python file in src compiles without SyntaxError or IndentationError."""
    src_dir = Path(__file__).resolve().parent.parent / "src"
    assert src_dir.exists()
    compiled = compileall.compile_dir(str(src_dir), force=True, quiet=True)
    assert compiled is True, "One or more Python files failed to compile in src!"


def test_cli_parser_and_imports():
    """Verifies that the CLI entrypoints and arguments can be parsed cleanly."""
    parser = build_parser()
    assert parser is not None
    # Test --version or invalid flag handling
    with pytest.raises(SystemExit):
        parser.parse_args(["--invalid-test-flag"])


def test_version_string():
    assert __version__ == "0.5.1"


def test_ruff_lint_check():
    """Runs ruff on both src/ and tests/ to catch syntax, indentation, and structural errors."""
    import subprocess
    import sys

    res = subprocess.run([sys.executable, "-m", "ruff", "check", "src", "tests"], capture_output=True, text=True)
    assert res.returncode == 0, f"Ruff linter failed with output:\n{res.stdout}\n{res.stderr}"


def test_project_configuration_files_validity():
    """Validates that pyproject.toml, docker-compose.yml, and static assets are structurally valid."""
    import tomllib

    root_dir = Path(__file__).resolve().parent.parent

    # 1. TOML validity & duplicate key check
    pyproject_file = root_dir / "pyproject.toml"
    assert pyproject_file.exists()
    content = pyproject_file.read_text(encoding="utf-8")
    parsed_toml = tomllib.loads(content)
    assert parsed_toml["project"]["version"] == "0.5.1"

    # 2. Docker Compose validity
    compose_file = root_dir / "docker-compose.yml"
    assert compose_file.exists()
    compose_text = compose_file.read_text(encoding="utf-8")
    assert "image: akashkece/evatorrent:0.5.1" in compose_text

    # 3. Web UI HTML template integrity
    index_html = root_dir / "src" / "evatorrent" / "web" / "static" / "index.html"
    assert index_html.exists()
    html_text = index_html.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html_text
    assert "v0.5.1 • Asyncio Core" in html_text

