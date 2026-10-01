class Momentum:
    def __init__(self, lookback, skip):
        if type(lookback) is not int or type(skip) is not int or not 0 <= skip < lookback:
            raise ValueError("Expected integer 0 <= skip < lookback")
        self.lookback, self.skip = lookback, skip
        self.warmup = lookback + 1

    def target(self, history):
        if len(history) < self.warmup:
            raise ValueError("Insufficient warmup")
        return float(history[-1-self.skip][1] > history[-1-self.lookback][1])
