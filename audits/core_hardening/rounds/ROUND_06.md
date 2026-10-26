# Round 06 — safe module configuration loading

- YAML configuration files receive a conservative size ceiling.
- Directories and non-files are rejected before parsing.
- `yaml.safe_load` remains the only YAML parser path.
