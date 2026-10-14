// agent-voice content script: watches the conversation on the page and tells the notch.
(function () {
  var site = AGENT_VOICE_SITES.find(function (s) { return s.host.test(location.hostname); });
  if (!site) return;

  function snapshot() {
    var all = document.querySelectorAll(site.reply);
    var last = all.length ? all[all.length - 1] : null;
    return { busy: !!document.querySelector(site.busy), text: last ? last.innerText : "" };
  }

  function send(msg) {
    msg.site = site.name;
    msg.conversation = location.pathname;
    msg.title = document.title;
    try { chrome.runtime.sendMessage(msg); } catch (e) { /* extension reloaded: nothing to do */ }
  }

  var tracker = agentVoiceTracker(send, Date.now);
  var conversation = location.pathname;
  tracker.prime(snapshot().text);

  // Battery: look only while the tab is visible, twice a second (a MutationObserver fires per token).
  setInterval(function () {
    if (document.hidden && !document.querySelector(site.busy)) return;
    if (location.pathname !== conversation) {  // another conversation opened: its history isn't new
      conversation = location.pathname;
      tracker = agentVoiceTracker(send, Date.now);
      tracker.prime(snapshot().text);
      return;
    }
    tracker.observe(snapshot());
  }, 500);
})();
