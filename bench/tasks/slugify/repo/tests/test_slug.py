from textkit import slugify


def test_simple():
    assert slugify("Hello World") == "hello-world"


def test_custom_sep():
    assert slugify("Hello World", sep="_") == "hello_world"
