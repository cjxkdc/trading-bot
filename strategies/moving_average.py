class MovingAverage:
    def __init__(self, fast, slow):
        if type(fast) is not int or type(slow) is not int or not 0 < fast < slow:
            raise ValueError("Expected integer 0 < fast < slow")
        self.fast, self.slow = fast, slow
        self.warmup = slow

    def target(self, history):
        if len(history) < self.warmup:
            raise ValueError("Insufficient warmup")
        prices = [p for _, p in history[-self.slow:]]
        return float(sum(prices[-self.fast:]) / self.fast > sum(prices) / self.slow)
