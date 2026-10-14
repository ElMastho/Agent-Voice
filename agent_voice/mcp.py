"""MCP server: any app that speaks the Model Context Protocol talks to you through agent-voice.

Antigravity, Claude Desktop, Cursor, Windsurf, VS Code, Codex, Gemini CLI... start `agent-voice mcp` and get
four tools: speak, notify, ask_user and set_status. stdio JSON-RPC 2.0, one message per line.

Stdlib only and never fatal, like the hooks: it runs inside the agent's process tree.
"""

from __future__ import annotations

import json
import os
import sys
import time

from agent_voice import __version__, client

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

# clientInfo.name as each app sends it -> our name for it (titles, colours, the agents row).
CLIENTS = {"antigravity": "antigravity", "claude-ai": "claude-desktop", "claude desktop": "claude-desktop",
           "visual studio code": "vscode", "vscode": "vscode", "cursor": "cursor", "windsurf": "windsurf",
           "codex": "codex", "gemini": "gemini", "zed": "zed", "cline": "cline", "claude-code": "claude"}

INSTRUCTIONS = (
    "agent-voice gives you a voice on the user's computer and a notch at the top of their screen. "
    "Call `set_status` with a few words when you start a task or a long step (\"Running the tests\"). "
    "At the end of every reply, call `speak` once with one short sentence (at most 25 words): what you did and "
    "anything the user must do next. Use `ask_user` instead of asking in the chat when you are blocked on a "
    "decision; the user answers by voice or with a click. Use `notify` for something worth a glance."
)
HOOKED_INSTRUCTIONS = (
    "agent-voice already reads your replies aloud. Call `set_status` with a few words when you start a task or a "
    "long step, and `ask_user` when you are blocked on a decision: the user answers by voice or with a click."
)

TOOLS = [
    {"name": "speak",
     "description": "Say a short message to the user out loud (shown under the notch when their voice is off). "
                    "Call it once at the end of every reply: one sentence, what happened and what they must do.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "What to say."}},
                     "required": ["text"]}},
    {"name": "notify",
     "description": "Show a short notification under the notch, without speaking. Set needs_you when you are "
                    "blocked waiting for the user: it joins their \"needs you\" list.",
     "inputSchema": {"type": "object", "properties": {
         "message": {"type": "string"}, "needs_you": {"type": "boolean", "default": False}},
         "required": ["message"]}},
    {"name": "ask_user",
     "description": "Ask the user a question out loud and in the notch, and wait for the answer (spoken, or a "
                    "click on an option). Returns their answer, or says they didn't answer in time.",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string"},
         "options": {"type": "array", "items": {"type": "string"}, "description": "Up to 5 short choices."},
         "timeout_seconds": {"type": "integer", "default": 120, "minimum": 10, "maximum": 600}},
         "required": ["question"]}},
    {"name": "set_status",
     "description": "Show what you are doing right now in the notch (a few words, e.g. \"Running the tests\"), "
                    "with a timer. Set done=true when you finish without a reply to speak.",
     "inputSchema": {"type": "object", "properties": {
         "label": {"type": "string"}, "done": {"type": "boolean", "default": False}}, "required": []}},
]


class Server:
    def __init__(self, client_name: str = "", hooked: bool = False):
        self.source = client_name
        self.hooked = hooked  # the agent already speaks through its hooks: no `speak` tool
        self.session = ""
        self._hwnd = 0

    def _name(self, info: dict) -> str:
        raw = str(info.get("name") or "").lower()
        return next((ours for key, ours in CLIENTS.items() if key in raw), raw.split()[0] if raw else "mcp")

    def _window(self) -> int:
        """The app's window (found from our parent processes), so your spoken reply can be typed there."""
        from agent_voice import winfocus

        if not winfocus.is_window(self._hwnd):
            self._hwnd = winfocus.app_window(os.getpid())
        return self._hwnd

    def handle(self, msg: dict) -> dict | None:
        method, params, mid = msg.get("method", ""), msg.get("params") or {}, msg.get("id")
        if mid is None:  # a notification (initialized, cancelled...): nothing to answer
            return None
        try:
            result = self._dispatch(method, params)
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        except Exception as exc:  # never crash the agent's session over a voice problem
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": repr(exc)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            self.source = self.source or self._name(params.get("clientInfo") or {})
            self.session = f"mcp-{self.source}-{os.getpid()}"
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agent-voice", "version": __version__},
                    "instructions": HOOKED_INSTRUCTIONS if self.hooked else INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t for t in TOOLS if not (self.hooked and t["name"] == "speak")]}
        if method == "tools/call":
            text, error = self.call(str(params.get("name", "")), params.get("arguments") or {})
            return {"content": [{"type": "text", "text": text}], "isError": error}
        raise KeyError(method)

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Run a tool: (text for the model, is_error)."""
        if name == "speak":
            text = str(args.get("text") or "").strip()
            if not text:
                return "Nothing to say.", True
            out = client.speak(text, source=self.source, final=True, session=self.session, hwnd=self._window())
            if out is None:
                return "agent-voice is not running.", True
            return ("Shown to the user (their voice is off)." if out.get("muted") else "Said to the user."), False
        if name == "notify":
            kind = "input" if args.get("needs_you") else "message"
            ok = client.request("/notify", {"kind": kind, "session": self.session, "agent": self.source,
                                            "text": str(args.get("message") or "")[:500]})
            return ("Shown." if ok is not None else "agent-voice is not running."), ok is None
        if name == "set_status":
            ok = client.request("/activity", {"session": self.session, "source": self.source,
                                              "label": str(args.get("label") or "Working")[:80],
                                              "done": bool(args.get("done"))})
            return ("Status updated." if ok is not None else "agent-voice is not running."), ok is None
        if name == "ask_user":
            return self._ask(args)
        return f"Unknown tool {name}.", True

    def _ask(self, args: dict) -> tuple[str, bool]:
        question = str(args.get("question") or "").strip()
        if not question:
            return "No question to ask.", True
        options = [str(o) for o in args.get("options") or [] if str(o).strip()][:5]
        self._window()  # an answer typed back goes to this app
        made = client.request("/ask", {"session": self.session, "source": self.source, "question": question,
                                       "options": options})
        if not made or not made.get("id"):
            return "agent-voice is not running: ask in the chat instead.", True
        deadline = time.time() + max(10, min(600, int(args.get("timeout_seconds") or 120)))
        while time.time() < deadline:
            state = client.request("/ask_state", {"id": made["id"]}) or {}
            if state.get("answer") is not None:
                return f"The user answered: {state['answer']}", False
            if not state.get("pending", True):
                return "The question was dismissed: ask in the chat instead.", False
            time.sleep(0.5)
        client.request("/ask_state", {"id": made["id"], "cancel": True})
        return "The user didn't answer in time: ask in the chat instead, or go on with your best judgement.", False


def run(name: str = "", hooked: bool = False) -> int:
    """`agent-voice mcp [--client NAME] [--hooks]`: serve MCP on stdin/stdout until the app closes it."""
    server = Server(name, hooked)
    if not client.is_running():
        client.start_detached(wait=0)  # start the voice now, without holding up the app
    out = sys.stdout.buffer  # bytes: no \r\n translation on Windows, always UTF-8
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            out.write(json.dumps(reply).encode("utf-8") + b"\n")
            out.flush()
    return 0
