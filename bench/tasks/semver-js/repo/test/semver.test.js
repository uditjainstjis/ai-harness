'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { compare, sort } = require('../src/semver');

test('core versions', () => {
  assert.strictEqual(compare('1.2.3', '1.2.4'), -1);
  assert.strictEqual(compare('2.0.0', '1.9.9'), 1);
  assert.strictEqual(compare('1.0.0', '1.0.0'), 0);
});

test('sort core', () => {
  assert.deepStrictEqual(sort(['1.10.0', '1.2.0', '1.9.0']), ['1.2.0', '1.9.0', '1.10.0']);
});
