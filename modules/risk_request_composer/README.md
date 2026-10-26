# Risk Request Composer

A deterministic Module API 1.0 adapter that closes the current v0.6 executable-chain gap:

`entry_model.plan + protection_target.plan + instrument.rules + portfolio.risk_snapshot -> risk.request`

It performs no sizing and emits no orders. `RiskSizingModule` remains the only owner of quantity approval. The default configuration fails closed when instrument or portfolio evidence is absent.
