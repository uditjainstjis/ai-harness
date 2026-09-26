from textkit import truncate_words


def test_truncate():
    assert truncate_words("a b c d", 2) == "a b..."
    assert truncate_words("a b", 2) == "a b"
