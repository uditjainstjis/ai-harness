slugify produces ugly slugs for real-world titles

`slugify` is supposed to produce clean URL slugs, but for anything other than two plain words it produces garbage:

```python
>>> from textkit import slugify
>>> slugify("  Hello,  World!  ")
'--hello---world---'
>>> slugify("Crème Brûlée recipe")
'cr-me-br-l-e-recipe'
```

Expected `'hello-world'` and `'creme-brulee-recipe'`: runs of separators should collapse into one, there should be no separator at the start or end, and accented letters should be transliterated to their ASCII base letter instead of being dropped. The custom `sep` argument must keep working.
