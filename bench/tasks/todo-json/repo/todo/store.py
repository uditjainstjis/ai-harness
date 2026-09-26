import json
import os

DEFAULT_PATH = os.path.expanduser("~/.todo.json")


class Store:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path) as fh:
            return json.load(fh)

    def save(self, items):
        with open(self.path, "w") as fh:
            json.dump(items, fh)
