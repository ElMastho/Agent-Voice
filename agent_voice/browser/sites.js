// agent-voice: what each chat site looks like, and when a reply is finished.
// The reply detection is pure (no DOM) so it can be tested on its own.

/* Each site: where the assistant's messages are, and the "stop generating" button while it writes.
   A reply is done once the stop button is gone and the text stopped changing; sites whose markup
   changes keep working on the second rule alone (the text stays still for a few seconds). */
var AGENT_VOICE_SITES = [
  { host: /(^|\.)chatgpt\.com$|(^|\.)chat\.openai\.com$/, name: "chatgpt",
    reply: '[data-message-author-role="assistant"]',
    busy: '[data-testid="stop-button"], button[aria-label*="Stop" i]' },
  { host: /(^|\.)claude\.ai$/, name: "claude-web",
    reply: "[data-is-streaming]",
    busy: '[data-is-streaming="true"], button[aria-label*="Stop" i]' },
  { host: /(^|\.)gemini\.google\.com$/, name: "gemini-web",
    reply: "model-response message-content, model-response",
    busy: 'button[aria-label*="Stop" i], .stop-icon' },
  { host: /(^|\.)aistudio\.google\.com$/, name: "aistudio",
    reply: '[data-turn-role="Model"], ms-chat-turn .model-prompt-container',
    busy: 'button[aria-label*="Stop" i], run-button .stop' },
  { host: /(^|\.)perplexity\.ai$/, name: "perplexity",
    reply: ".prose",
    busy: 'button[aria-label*="Stop" i]' },
  { host: /(^|\.)copilot\.microsoft\.com$/, name: "copilot",
    reply: '[data-content="ai-message"], [class*="ai-message"]',
    busy: 'button[aria-label*="Stop" i]' },
  { host: /(^|\.)chat\.mistral\.ai$/, name: "mistral",
    reply: '[data-message-author-role="assistant"], .prose',
    busy: 'button[aria-label*="Stop" i]' },
  { host: /(^|\.)chat\.deepseek\.com$/, name: "deepseek",
    reply: ".ds-markdown",
    busy: 'div[role="button"] svg rect, button[aria-label*="Stop" i]' },
];

var AGENT_VOICE_QUIET_MS = 1500;   // after the stop button goes: the last tokens land
var AGENT_VOICE_STILL_MS = 4000;   // no stop button found: this long without a change ends it

/* Feed it what the page shows, a few times a second; it calls send() with
   {type: "start"}, {type: "status", label}, {type: "reply", text}. */
function agentVoiceTracker(send, now) {
  var state = { working: false, last: null, changedAt: 0, spoken: null, statusAt: 0, sawBusy: false };
  return {
    // First look at the page: what's already there isn't new.
    prime: function (text) { state.spoken = text; state.last = text; },
    observe: function (snap) {
      var t = now();
      var text = (snap.text || "").trim();
      if (text !== state.last) { state.last = text; state.changedAt = t; }
      var fresh = text && text !== state.spoken;
      if (!state.working && (snap.busy || fresh)) {
        state.working = true; state.sawBusy = !!snap.busy; state.statusAt = t;
        send({ type: "start" });
      }
      if (!state.working) return;
      if (snap.busy) state.sawBusy = true;
      if (snap.busy || t - state.changedAt < AGENT_VOICE_QUIET_MS) {
        if (t - state.statusAt > 3000 && text) {
          state.statusAt = t;
          var line = text.split("\n").filter(function (l) { return l.trim(); }).pop() || "";
          send({ type: "status", label: line.slice(0, 80) });
        }
        return;
      }
      if (!state.sawBusy && t - state.changedAt < AGENT_VOICE_STILL_MS) return;
      state.working = false;
      if (fresh) { state.spoken = text; send({ type: "reply", text: text }); }
    },
  };
}

if (typeof module !== "undefined") module.exports = { AGENT_VOICE_SITES: AGENT_VOICE_SITES, agentVoiceTracker: agentVoiceTracker };
