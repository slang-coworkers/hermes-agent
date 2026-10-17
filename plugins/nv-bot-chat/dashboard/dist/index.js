(function () {
  "use strict";
  // nv-bot-chat dashboard tab — lists each served profile's hidden canonical
  // "Bot Chat" and opens one read-only, via /api/plugins/nv-bot-chat.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect } = SDK.hooks;
  const C = SDK.components || {};
  const BASE = "/api/plugins/nv-bot-chat";

  function api(path) {
    // Host SDK fetchJSON handles auth (loopback token vs gated cookie).
    return SDK.fetchJSON(BASE + path);
  }

  function errorText(e) {
    return String(e && e.message ? e.message : e);
  }

  function Btn(props, label) {
    const Button = C.Button || "button";
    return h(Button, Object.assign({ type: "button" }, props), label);
  }

  function messageText(m) {
    const content = m.display_content != null ? m.display_content : m.content;
    if (content == null) return "";
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content
        .map(function (part) {
          if (typeof part === "string") return part;
          return part && typeof part.text === "string" ? part.text : "";
        })
        .filter(Boolean)
        .join("\n");
    }
    return JSON.stringify(content);
  }

  function Transcript(props) {
    const chat = props.chat;
    const [messages, setMessages] = useState(null);
    const [error, setError] = useState(null);

    useEffect(function () {
      let alive = true;
      api("/bot-chats/" + encodeURIComponent(chat.profile) + "/" +
          encodeURIComponent(chat.session_id) + "/messages")
        .then(function (d) { if (alive) setMessages(d.messages || []); })
        .catch(function (e) { if (alive) setError(errorText(e)); });
      return function () { alive = false; };
    }, [chat.profile, chat.session_id]);

    const shown = (messages || []).filter(function (m) {
      return m.display_kind !== "hidden" && messageText(m);
    });

    let body;
    if (error) {
      body = h("p", { className: "nv-bot-chat-error" }, "Failed to open Bot Chat: " + error);
    } else if (messages === null) {
      body = h("p", { className: "nv-bot-chat-loading" }, "Loading transcript…");
    } else if (shown.length === 0) {
      body = h("p", { className: "nv-bot-chat-empty" }, "This Bot Chat has no messages yet.");
    } else {
      body = h("ol", { className: "nv-bot-chat-messages" }, shown.map(function (m, i) {
        return h("li", {
          key: m.id != null ? m.id : i,
          className: "nv-bot-chat-message",
          "data-role": m.role,
        },
          h("span", { className: "nv-bot-chat-role" }, m.role),
          h("div", { className: "nv-bot-chat-content" }, messageText(m)));
      }));
    }

    return h("div", {
      className: "nv-bot-chat-transcript",
      "data-profile": chat.profile,
      "data-session-id": chat.session_id,
    },
      h("div", { className: "nv-bot-chat-toolbar" },
        Btn({ onClick: props.onBack, className: "nv-bot-chat-back" }, "← All Bot Chats"),
        h("h2", { className: "nv-bot-chat-heading" }, chat.profile + " · Bot Chat")),
      body);
  }

  function BotChatList(props) {
    if (props.chats.length === 0) {
      return h("p", { className: "nv-bot-chat-empty" }, "No served profile has a Bot Chat yet.");
    }
    return h("ul", { className: "nv-bot-chat-list" }, props.chats.map(function (chat) {
      return h("li", { key: chat.profile, className: "nv-bot-chat-entry", "data-profile": chat.profile },
        h("button", {
          type: "button",
          className: "nv-bot-chat-open",
          onClick: function () { props.onOpen(chat); },
        },
          h("span", { className: "nv-bot-chat-profile" }, chat.profile),
          h("span", { className: "nv-bot-chat-title" }, chat.title || "Bot Chat"),
          h("span", { className: "nv-bot-chat-count" },
            String(chat.message_count || 0) + " messages")));
    }));
  }

  function BotChatPage() {
    const [chats, setChats] = useState(null);
    const [error, setError] = useState(null);
    const [open, setOpen] = useState(null);

    useEffect(function () {
      let alive = true;
      api("/bot-chats")
        .then(function (d) { if (alive) setChats(d.bot_chats || []); })
        .catch(function (e) { if (alive) setError(errorText(e)); });
      return function () { alive = false; };
    }, []);

    if (open) {
      return h("div", { className: "nv-bot-chat" },
        h(Transcript, { chat: open, onBack: function () { setOpen(null); } }));
    }

    let body;
    if (error) {
      body = h("p", { className: "nv-bot-chat-error" }, "Failed to load Bot Chats: " + error);
    } else if (chats === null) {
      body = h("p", { className: "nv-bot-chat-loading" }, "Loading Bot Chats…");
    } else {
      body = h(BotChatList, { chats: chats, onOpen: setOpen });
    }
    return h("div", { className: "nv-bot-chat" },
      h("h2", { className: "nv-bot-chat-heading" }, "Bot Chats"),
      h("p", { className: "nv-bot-chat-sub" },
        "The canonical Bot Chat of each profile this gateway serves (read-only)."),
      body);
  }

  window.__HERMES_PLUGINS__.register("nv-bot-chat", BotChatPage);
})();
