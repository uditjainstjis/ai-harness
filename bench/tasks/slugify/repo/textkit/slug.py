import re

_NON_WORD = re.compile(r"[^a-z0-9]")


def slugify(text, sep="-"):
    """Turn arbitrary text into a URL slug.

    >>> slugify("Hello World")
    'hello-world'
    """
    text = text.lower()
    return _NON_WORD.sub(sep, text)
