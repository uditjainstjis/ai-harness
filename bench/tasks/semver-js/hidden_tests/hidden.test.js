'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { compare, sort } = require('../src/semver');

test('prerelease lower than release', () => {
  assert.strictEqual(compare('1.0.0-alpha', '1.0.0'), -1);
  assert.strictEqual(compare('1.0.0', '1.0.0-alpha'), 1);
});

test('numeric identifiers compare numerically', () => {
  assert.strictEqual(compare('1.0.0-alpha.10', '1.0.0-alpha.2'), 1);
  assert.strictEqual(compare('1.0.0-beta.2', '1.0.0-beta.11'), -1);
});

test('numeric lower than alphanumeric', () => {
  assert.strictEqual(compare('1.0.0-alpha.1', '1.0.0-alpha.beta'), -1);
});

test('build metadata ignored and hyphenated prerelease', () => {
  assert.strictEqual(compare('1.0.0+build.5', '1.0.0'), 0);
  assert.strictEqual(compare('1.0.0-rc-1', '1.0.0-rc-1+exp.sha'), 0);
  assert.strictEqual(compare('1.0.0-rc-1', '1.0.0'), -1);
});

test('spec ordering', () => {
  const ordered = ['1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta', '1.0.0-beta', '1.0.0-beta.2', '1.0.0-beta.11', '1.0.0-rc.1', '1.0.0'];
  const shuffled = [...ordered].reverse();
  assert.deepStrictEqual(sort(shuffled), ordered);
});
