# Round 29 — atomic fill/trade event batches

Event stores now validate and append batches before mutating in-memory indexes. Position close records fill and trade as one accepted batch before cash, fill sequence or position state changes. A rejected batch leaves journal and broker state untouched.
