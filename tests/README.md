# Test layout

Reusable test source is versioned. Generated data, reports and workstation-only tests are ignored.

- `test_*.py`: reusable regression tests.
- `local/`: tests of ignored historical diagnostic runners; local only.
- `data/`: retained fixtures; legacy smoke COGs are in `data/smoke_cache/`.
- `reports/<run-id>/`: durable pytest output, separate from disposable data.
- Scratch data: repository `.test-tmp/<run-id>/` (ignored).

Run with the project's Python 3.12 environment:

```powershell
python -B tests/run.py
```

The runner creates a unique scratch directory, routes `tempfile` and pytest into
it, disables caches, saves reports here and returns pytest's exit code. It does
not delete earlier reports or fixtures. Direct pytest runs must provide their
own dedicated `--basetemp` and TEMP/TMP directories.

Quality pairing tests need GRiTS-Resolver installed into the **same Python** that
runs pytest. A sibling folder alone is not enough. Keep it optional for standalone
S2 installations; require it for downstream integration acceptance:

```powershell
python -m pip install -e ".[dev]" -e ../GRiTS-Resolver
python -B tests/run.py --integration
```

Editable installation uses that checkout directly; no source copy is required.
The integration command prints the interpreter, installed Resolver version and
module location, fails if its dependencies cannot be imported, and runs the four
native/reprojected-window and SCL-pairing cases. A missing dependency may still
skip these tests in the default standalone run; that is not integration acceptance.

The default test run includes local tests when present; a public checkout does not require them.
