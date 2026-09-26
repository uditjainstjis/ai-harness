from textkit import slugify


def test_collapse_and_strip():
    assert slugify("  Hello,  World!  ") == "hello-world"


def test_accents():
    assert slugify("Crème Brûlée recipe") == "creme-brulee-recipe"


def test_custom_sep_collapse():
    assert slugify("--Hello   World--", sep="_") == "hello_world"


def test_plain():
    assert slugify("Hello World") == "hello-world"


def test_empty():
    assert slugify("!!!") == ""
