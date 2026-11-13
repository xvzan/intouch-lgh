# Public repository and validation policy

This repository contains the Python core, examples, tests, and two pairs of
public third-party test archives identified in `THIRD_PARTY_NOTICES.md`.

No private industrial archive corpus, customer data, logs, or release-audit
reports are part of the repository.

To exercise an independent local LGH/IDX corpus without publishing it:

```bash
INTOUCH_LGH_CORPUS_DIR=/your/local/private/data python -m pytest -q
python scripts/validate_corpus.py /your/local/private/data \
  --output /your/local/private/data/corpus_validation_report.json
```

Windows PowerShell:

```powershell
$env:INTOUCH_LGH_CORPUS_DIR = 'D:\local\private\data'
python -m pytest -q
$report = Join-Path $env:INTOUCH_LGH_CORPUS_DIR 'corpus_validation_report.json'
python .\scripts\validate_corpus.py $env:INTOUCH_LGH_CORPUS_DIR --output $report
Remove-Item Env:INTOUCH_LGH_CORPUS_DIR
```

When `--output` is omitted, corpus-validation and audit reports are written
inside the supplied corpus directory, not the repository working directory.
The repository `.gitignore` also excludes the standard report filenames as a
second line of defense. Prefer keeping private corpora and their reports
outside the repository entirely.

Before publishing, run your locally maintained pre-publication privacy scanner
against the repository. The scanner and its private detection rules are
maintained outside this repository and must never be committed.

Manually inspect all files, Git staged changes, Git author/email, sample
licensing, and binary attachments. Automated scanning is not proof that a
repository contains no secrets.
