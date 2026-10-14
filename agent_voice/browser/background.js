// agent-voice: passes what the pages see to the agent-voice app on this computer (native messaging).
var port = null;

function connect() {
  if (port) return port;
  try {
    port = chrome.runtime.connectNative("com.agentvoice.bridge");
    port.onDisconnect.addListener(function () { port = null; });
  } catch (e) {
    port = null;
  }
  return port;
}

chrome.runtime.onMessage.addListener(function (msg, sender) {
  if (!sender.tab) return;
  msg.tab = sender.tab.id;
  var p = connect();
  if (p) {
    try { p.postMessage(msg); } catch (e) { port = null; }
  }
});
