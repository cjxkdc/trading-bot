import csv
import math
from datetime import date


class CsvData:
    def __init__(self, path):
        self.path = path

    def load(self):
        with open(self.path, encoding="utf-8-sig", newline="") as handle:
            rows = [(date.fromisoformat(r["date"]), float(r["total_return_index"]))
                    for r in csv.DictReader(handle)]
        if not rows or any(not math.isfinite(p) or p <= 0 for _, p in rows):
            raise ValueError("Empty or invalid total-return data")
        if any(a[0] >= b[0] for a, b in zip(rows, rows[1:])):
            raise ValueError("Dates must be unique and increasing")
        return tuple(rows)
