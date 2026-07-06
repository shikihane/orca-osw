import sys
import unittest
from unittest.mock import patch
from io import StringIO
import importlib

from osw.deps import check_deps


class TestCheckDeps(unittest.TestCase):
    """Tests for the dependency checker."""

    def test_both_deps_present(self):
        """Test that check_deps() returns without error when both deps are present."""
        # This test runs with both anyio and typer installed (they should be)
        # check_deps() should return None without raising an exception
        result = check_deps()
        self.assertIsNone(result)

    def test_anyio_missing(self):
        """Test that missing anyio is detected and proper error is shown."""
        # Save the real __import__
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def import_side_effect(name, *args, **kwargs):
            if name == "anyio":
                raise ImportError(f"No module named '{name}'")
            # Use the real import for everything else
            return real_import(name, *args, **kwargs)

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with patch("sys.exit") as mock_exit:
                with patch("builtins.__import__", side_effect=import_side_effect):
                    try:
                        check_deps()
                    except SystemExit:
                        pass

                    mock_exit.assert_called_once_with(1)
                    stderr_output = mock_stderr.getvalue()
                    self.assertIn("Missing Python dependencies: anyio", stderr_output)
                    self.assertIn("python -m pip install anyio typer", stderr_output)

    def test_typer_missing(self):
        """Test that missing typer is detected and proper error is shown."""
        # Save the real __import__
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def import_side_effect(name, *args, **kwargs):
            if name == "typer":
                raise ImportError(f"No module named '{name}'")
            # Use the real import for everything else
            return real_import(name, *args, **kwargs)

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with patch("sys.exit") as mock_exit:
                with patch("builtins.__import__", side_effect=import_side_effect):
                    try:
                        check_deps()
                    except SystemExit:
                        pass

                    mock_exit.assert_called_once_with(1)
                    stderr_output = mock_stderr.getvalue()
                    self.assertIn("Missing Python dependencies: typer", stderr_output)
                    self.assertIn("python -m pip install anyio typer", stderr_output)

    def test_both_deps_missing(self):
        """Test that missing both anyio and typer are detected."""
        # Save the real __import__
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def import_side_effect(name, *args, **kwargs):
            if name in ("anyio", "typer"):
                raise ImportError(f"No module named '{name}'")
            # Use the real import for everything else
            return real_import(name, *args, **kwargs)

        with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
            with patch("sys.exit") as mock_exit:
                with patch("builtins.__import__", side_effect=import_side_effect):
                    try:
                        check_deps()
                    except SystemExit:
                        pass

                    mock_exit.assert_called_once_with(1)
                    stderr_output = mock_stderr.getvalue()
                    self.assertIn("Missing Python dependencies: anyio, typer", stderr_output)
                    self.assertIn("python -m pip install anyio typer", stderr_output)


if __name__ == "__main__":
    unittest.main()
