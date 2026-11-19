"""Run with QGIS Python; works with either installed plugin folder name."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "forestry_operations_lite", root / "__init__.py",
    submodule_search_locations=[str(root)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
suite = unittest.defaultTestLoader.discover(str(root / "test"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
