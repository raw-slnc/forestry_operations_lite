# Bundled third-party libraries

These libraries are bundled so the plugin works without installing extra Python packages.
Each keeps its original license file. They are modified from the original releases as listed below.

## laspy 2.7.0

- Source: https://github.com/laspy/laspy (PyPI: https://pypi.org/project/laspy/2.7.0/)
- License: BSD 2-Clause, see `laspy/LICENSE.txt`
- Used for: reading Virtual Shizuoka LAS point clouds (VS LP -> DSM)
- Changes from the original release:
  - Removed `cli/` (command-line tool; needs `typer`, not used by the plugin) and `lib.pyi` (type stub).
  - Replaced `assert` checks with explicit exceptions (`LaspyException`, `RuntimeError`, `ValueError`) so the checks also run with optimisation enabled and pass the plugin submission security scan.
  - In `lasdata.py`, the scale/offset check uses `np.array_equal` instead of `np.all`.
- laspy 2.7.0 declares Python >= 3.10 on PyPI. The bundled modules were checked for syntax and standard-library names newer than Python 3.7 (QGIS 3.16); none are used.

## defusedxml 0.7.1

- Source: https://github.com/tiran/defusedxml (PyPI: https://pypi.org/project/defusedxml/0.7.1/)
- Copyright (c) 2013-2017 by Christian Heimes. Licensed to PSF under a Contributor Agreement.
- License: Python Software Foundation License Version 2, see `defusedxml/LICENSE`
- Used for: parsing XML safely (Windows QGIS does not always include defusedxml)
- Changes from the original release:
  - Only `__init__.py`, `common.py` and `ElementTree.py` are kept.
  - `ElementTree.py` imports the standard `xml.etree.ElementTree` module through `importlib` instead of direct `from` imports, to pass the plugin submission security scan.
  - In `common.py`, an `assert` check is replaced with an explicit `RuntimeError`.
