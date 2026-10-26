# Configuration reference

Configuration is frozen and fingerprinted. Unknown constructor arguments fail naturally; invalid values raise `WaveAnalysisConfigurationError`. A checkpoint with another algorithmic fingerprint is rejected.

Parameter groups: timeframes, detector method, fractal/ZigZag/ATR thresholds, minimum leg geometry, equal-level tolerance, classification thresholds, Elliott controls, retained-state bounds, `feature_snapshot_limit`, gap recovery, provisional publication and diagnostics.

Financial thresholds are canonical Decimal strings in manifests and fingerprints.
