# Capability matrix — package 0.4.0

`TESTED` means contract, parser and fault-injection tests passed locally. It does not mean
real official-network acceptance has passed.

| Adapter | Market | Data type | Source | Local status | Limits |
|---|---|---|---|---|---|
| Fake | Spot | klines, metadata | deterministic fake REST | tested | test-only |
| Fake | USD-M perpetual | klines, funding, mark, index, premium, open interest, metadata | deterministic fake REST | tested | test-only |
| Binance | Spot | traded klines | official archive + REST | contract-tested | live DNS unavailable here |
| Binance | Spot | instrument/exchange metadata | official REST | contract-tested | live DNS unavailable here |
| Binance | USD-M perpetual | traded klines | official archive + REST | contract-tested | live DNS unavailable here |
| Binance | USD-M perpetual | funding rate | official REST | contract-tested | event cadence may vary |
| Binance | USD-M perpetual | mark-price klines | official REST | contract-tested | fixed supported intervals |
| Binance | USD-M perpetual | index-price klines | official REST | contract-tested | fixed supported intervals |
| Binance | USD-M perpetual | premium-index klines | official REST | contract-tested | fixed supported intervals |
| Binance | USD-M perpetual | open-interest history | official REST | contract-tested | official recent-history retention window |
| Binance | USD-M perpetual | instrument/exchange metadata | official REST | contract-tested | live DNS unavailable here |

Unsupported and not advertised:

- COIN-M delivery/perpetual;
- options;
- trades, aggregate trades and liquidation streams;
- arbitrary third-party data mirrors;
- live market data.

The Binance adapter remains at `AdapterMaturity.TESTED`. Promotion to `PRODUCTION`
requires real archive/REST acceptance, multi-year evidence and cross-source checks.
