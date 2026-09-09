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
    assert __version__ == "0.5.0"
