"""Passive comparison inputs for the offline research engine; not strategy plugins."""


class BuyAndHoldBenchmark:
    warmup = 0

    def target(self, history):
        return 1.0
