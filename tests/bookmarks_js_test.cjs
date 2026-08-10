const assert = require("node:assert/strict");
const bookmarks = require("../app/static/bookmarks.js");

assert.equal(bookmarks.formatBookmarkTime(112 * 60 + 28), "1:52:28");
assert.equal(bookmarks.parseBookmarkTime("2:25:13"), 8713);
assert.equal(bookmarks.parseBookmarkTime("25:13"), 1513);
assert.equal(bookmarks.parseBookmarkTime("2:65:13"), null);
assert.equal(bookmarks.normalizeBookmarkShift(60.4), 60);
assert.equal(bookmarks.effectiveBookmarkOffset(1, -5, 20), 0);
assert.equal(bookmarks.currentBookmarkIndex([
  { kind: "range", effective_offset_seconds: 10, effective_end_offset_seconds: 40 },
  { kind: "range", effective_offset_seconds: 20, effective_end_offset_seconds: 30 },
], 25), 1);
assert.equal(bookmarks.shouldHandleBookmarkShortcut({ key: "b", target: { tagName: "DIV" } }), true);
assert.equal(bookmarks.shouldHandleBookmarkShortcut({ key: "b", target: { tagName: "INPUT" } }), false);
