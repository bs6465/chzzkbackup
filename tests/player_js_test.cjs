const assert = require("node:assert/strict");
const player = require("../app/static/player.js");

assert.equal(player.formatTimelineTime(0), "00:00");
assert.equal(player.formatTimelineTime(112 * 60 + 28), "1:52:28");
assert.equal(player.normalizeChatDelay(60.4), 60);
assert.equal(player.normalizeChatDelay(-61), -60);
assert.equal(player.adjustedChatOffset(10, 2.5, 100), 12.5);
assert.equal(player.adjustedChatOffset(10, -20, 100), 0);
assert.equal(player.clampedSeekTime(3, -5, 100), 0);
assert.equal(player.clampedSeekTime(98, 10, 100), 100);
assert.equal(player.chatScrollTarget(100, 200, 500, 450, 40, 2000), 120);
assert.equal(player.chatScrollTarget(0, 200, 500, 100, 40, 2000), 0);
assert.equal(player.chatScrollTarget(1400, 200, 500, 650, 40, 1800), 1300);
assert.equal(player.seekStepForEvent({ key: "ArrowLeft", shiftKey: false }), -5);
assert.equal(player.seekStepForEvent({ key: "ArrowRight", shiftKey: true }), 10);
assert.equal(player.shouldHandleSeekShortcut({ key: "ArrowRight", target: { tagName: "VIDEO" } }), true);
assert.equal(player.shouldHandleSeekShortcut({ key: "ArrowRight", target: { tagName: "INPUT" } }), false);
