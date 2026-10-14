const assert = require("assert");
const { AGENT_VOICE_SITES, agentVoiceTracker } = require(process.argv[2]);
const pick = (h) => AGENT_VOICE_SITES.find((s) => s.host.test(h));
assert.equal(pick("chatgpt.com").name, "chatgpt");
assert.equal(pick("claude.ai").name, "claude-web");
assert.equal(pick("gemini.google.com").name, "gemini-web");
assert.equal(pick("evilclaude.ai"), undefined);
let t = 0, sent = [];
const tr = agentVoiceTracker((m) => sent.push(m), () => t);
tr.prime("Old answer.");
tr.observe({ busy: false, text: "Old answer." }); t += 5000;
tr.observe({ busy: false, text: "Old answer." });
assert.deepEqual(sent, []);                       // nothing new: silent
tr.observe({ busy: true, text: "Old answer." });  // you sent a prompt: it writes
assert.deepEqual(sent.map((m) => m.type), ["start"]);
for (let i = 0; i < 10; i++) { t += 500; tr.observe({ busy: true, text: "New answer, part " + i }); }
assert.ok(sent.some((m) => m.type === "status"));
t += 500; tr.observe({ busy: false, text: "New answer, done." });
t += 1000; tr.observe({ busy: false, text: "New answer, done." });
assert.ok(!sent.some((m) => m.type === "reply")); // the last tokens may still land
t += 600; tr.observe({ busy: false, text: "New answer, done." });
assert.deepEqual(sent[sent.length - 1], { type: "reply", text: "New answer, done." });
// A site whose stop button we can't see: the text going still for 4 s ends it.
sent = []; const tr2 = agentVoiceTracker((m) => sent.push(m), () => t); tr2.prime("");
tr2.observe({ busy: false, text: "Hello" }); t += 2000; tr2.observe({ busy: false, text: "Hello there" });
t += 2000; tr2.observe({ busy: false, text: "Hello there" });
assert.ok(!sent.some((m) => m.type === "reply"));
t += 2500; tr2.observe({ busy: false, text: "Hello there" });
assert.deepEqual(sent.map((m) => m.type), ["start", "reply"]);
console.log("sites.js ok");
