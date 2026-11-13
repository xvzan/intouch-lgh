# Third-party notices

## PKUbuntu/intouch-dumper

Repository: https://github.com/PKUbuntu/intouch-dumper

The following files in `tests/fixtures/` are exact, unchanged copies of files
from the upstream repository's `data/` directory and root directory. Their
Git blob identifiers were checked against the upstream repository:

| Local file | Upstream path | Upstream Git blob |
|---|---|---|
| `tests/fixtures/24072200.idx` | `data/24072200.idx` | `00a16939d8de89ebec8eb281abd837ba69d18ff5` |
| `tests/fixtures/24072200.lgh` | `data/24072200.lgh` | `2d7873718396496b3665ac767a0d17374a6c88bc` |
| `tests/fixtures/24072300.idx` | `data/24072300.idx` | `62f6096dff10d84a58529e29387fd5a5f1867d45` |
| `tests/fixtures/24072300.lgh` | `data/24072300.lgh` | `cd197645a59145d32be142261cb28a239c4a64e6` |
| `tests/fixtures/example_output.txt` | `example_output.txt` | `b0345c6bd350a1808f22d9fd832062d2835026e3` |

The upstream repository's `LICENSE` identifies the copyright holder as
`Boarmon` and grants the MIT License. Its license text is preserved verbatim
in `tests/fixtures/UPSTREAM_LICENSE.txt`; that license text is also included
as a distribution license file in the wheel. The MIT terms continue to apply to
the listed upstream materials; the project-wide Apache License 2.0 does not
replace or remove those third-party terms.

The `intouch-lgh` implementation was informed by the public format research
in `PKUbuntu/intouch-dumper`, including its README and `dump_data.py`.
A source review compared that upstream script with this project's
`intouch_lgh/reader.py`; no substantial verbatim source-code passage was
identified. The upstream project does not contain this project's IDX tree
reader, Archive API, batch model, NumPy/Arrow adapters, or Parquet exporter.

This provenance note is a technical description of the reviewed files, not
legal advice or a claim that every historical development step has been
independently verified.
