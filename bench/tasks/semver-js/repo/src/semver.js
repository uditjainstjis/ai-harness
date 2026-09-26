'use strict';

function parse(version) {
  const [core, pre] = version.split('-');
  const [major, minor, patch] = core.split('.').map(Number);
  return { major, minor, patch, pre: pre ? pre.split('.') : [] };
}

// Returns -1, 0 or 1.
function compare(a, b) {
  const x = parse(a);
  const y = parse(b);
  for (const k of ['major', 'minor', 'patch']) {
    if (x[k] !== y[k]) return x[k] < y[k] ? -1 : 1;
  }
  const n = Math.max(x.pre.length, y.pre.length);
  for (let i = 0; i < n; i++) {
    if (x.pre[i] === y.pre[i]) continue;
    if (x.pre[i] === undefined) return -1;
    if (y.pre[i] === undefined) return 1;
    return x.pre[i] < y.pre[i] ? -1 : 1;
  }
  return 0;
}

function sort(versions) {
  return [...versions].sort(compare);
}

module.exports = { parse, compare, sort };
