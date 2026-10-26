# Declared dependency inputs

`scripts/platform_dependency_inputs.py` emits a deterministic report of source-declared Python and npm package inputs for one exact, clean source commit:

```bash
python -m scripts.platform_dependency_inputs \
  --source-root . \
  --source-commit <40-character-commit> \
  --output /tmp/dependency-inputs.json
```

Keep generated reports outside `source_root`. The generator requires a clean source tree, and an in-tree output would make a subsequent validation run reject that tree as dirty.

The report includes each discovered Python distribution's descriptor path and SHA-256, static `requires-python`, build-system declarations, base requirements, and named optional-dependency groups. Requirement strings, group names, ordering, duplicates, markers, URLs, ranges, and requested extras remain as declared. Dynamic fields are marked `unsupported_dynamic`; missing build-system metadata is marked incomplete. npm package and lock paths and hashes are included, along with the source package's dependency categories and engine, peer, and platform metadata. The generator checks the npm lock against the package descriptor using the release validator's existing binding rules.

`validate_dependency_input_report(report, source_root)` checks that the report matches the declarations at the current clean source HEAD. It rejects reports for a different commit or reports that differ from source declarations.

Owner decision: Signal Bot dependency support belongs to the full PAPER/research support profile and its common lock family, while process activation is optional and configuration-controlled. The selected `signal-bot` extra contributes to the shared runtime requirements; `enabled: false` disables the process before credentials or client construction, but does not describe a supported installation that omits these dependencies. Supporting a reduced installation would require a versioned install-selection contract, dependency closures, and matching reconstruction evidence. The profile scope retains all 26 Python distribution providers and the selected functional extras. The v3 manifest schema binds a per-provider extra selection and runtime-resolution receipts, but this source-declaration report does not resolve dependencies, produce those receipts, or establish authenticated resolver execution. The profile and locks remain incomplete; exact CPython patches, indexes, toolchains, artifact references, and six typed bootstrap/build/runtime lock identities across CPython 3.12 and 3.13 remain open.

This is a declared-input inventory only. It does not evaluate markers, select extras or platforms, choose versions or an index policy, resolve dependencies, or produce resolver lock artifacts or reconstruction receipts. It does not satisfy the release gates for resolved locks or clean Python 3.12/3.13 reconstruction.
