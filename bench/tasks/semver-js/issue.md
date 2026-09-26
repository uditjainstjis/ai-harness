Pre-release versions sort in the wrong order

`compare` does not follow the SemVer 2.0.0 precedence rules for pre-release versions:

```js
compare('1.0.0-alpha', '1.0.0')            // returns 1, expected -1: a pre-release is LOWER than the release
compare('1.0.0-alpha.10', '1.0.0-alpha.2') // returns -1, expected 1: numeric identifiers compare numerically
compare('1.0.0-alpha.1', '1.0.0-alpha.beta') // should be -1: numeric identifiers are lower than alphanumeric ones
```

Also, build metadata (`1.0.0+build.5`) must be ignored for precedence, and a version with a hyphen inside a pre-release identifier (`1.0.0-rc-1`) must parse correctly. `sort` should then order e.g. `1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-alpha.beta < 1.0.0-beta < 1.0.0-beta.2 < 1.0.0-beta.11 < 1.0.0-rc.1 < 1.0.0`.
