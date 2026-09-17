const $ = (selector) => document.querySelector(selector);
const state = {
  conversations: [],
  conversationNext: null,
  active: null,
  messageNext: null,
  messageNewer: null,
  messageLoading: false,
  loading: false,
  searchTimer: null,
  threadMatches: [],
  threadMatchIndex: -1,
  threadMatchTotal: 0,
  threadMatchNext: null,
  gallery: {
    conversation: null, type: "all", items: [], next: null, loading: false,
    windowStart: 0,
  },
  viewer: { items: [], index: -1, touchStartX: null },
  inGlobalSearch: false,
};

const els = {
  list: $("#conversationList"),
  loadConversations: $("#loadConversations"),
  searchForm: $("#searchForm"),
  searchInput: $("#searchInput"),
  clearSearch: $("#clearSearch"),
  filterRow: $("#filterRow"),
  closeResults: $("#closeResults"),
  thread: $("#thread"),
  person: $("#threadPerson"),
  welcome: $("#welcome"),
  messages: $("#messages"),
  scroll: $("#messageScroll"),
  loadOlder: $("#loadOlder"),
  back: $("#backButton"),
  theme: $("#themeButton"),
  threadSearchButton: $("#threadSearchButton"),
  threadSearch: $("#threadSearch"),
  threadSearchInput: $("#threadSearchInput"),
  closeThreadSearch: $("#closeThreadSearch"),
  matchNav: $("#matchNav"),
  matchStatus: $("#matchStatus"),
  newerMatch: $("#newerMatch"),
  olderMatch: $("#olderMatch"),
  globalAttachments: $("#globalAttachmentsButton"),
  threadAttachments: $("#threadAttachmentsButton"),
  gallery: $("#gallery"),
  galleryTitle: $("#galleryTitle"),
  galleryFilters: $("#galleryFilters"),
  galleryScroll: $("#galleryScroll"),
  galleryGrid: $("#galleryGrid"),
  galleryLoading: $("#galleryLoading"),
  closeGallery: $("#closeGallery"),
  viewer: $("#viewer"),
  viewerContent: $("#viewerContent"),
  closeViewer: $("#closeViewer"),
  viewerCounter: $("#viewerCounter"),
  viewerName: $("#viewerName"),
  viewerMeta: $("#viewerMeta"),
  previousAttachment: $("#previousAttachment"),
  nextAttachment: $("#nextAttachment"),
  showInConversation: $("#showInConversation"),
  toast: $("#toast"),
};

const api = async (path) => {
  const response = await fetch(path);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  return response.json();
};

const escapeHtml = (value = "") => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
}[char]));

const initials = (title = "?") => title.split(/[\s,&+]+/).filter(Boolean)
  .slice(0, 2).map((word) => word[0]?.toUpperCase()).join("") || "?";

const avatarColor = (text) => {
  let hash = 0;
  for (const char of text) hash = (hash * 31 + char.charCodeAt(0)) | 0;
  return `hsl(${Math.abs(hash) % 360} 36% 43%)`;
};

const avatar = (conversation, small = false) => `
  <span class="avatar ${small ? "small" : ""}" style="--avatar:${avatarColor(conversation.title)}">
    ${conversation.avatar_path
      ? `<img src="${escapeHtml(conversation.avatar_path)}" alt="" loading="lazy">`
      : escapeHtml(initials(conversation.title))}
  </span>`;

const formatListTime = (timestamp) => {
  const date = new Date(timestamp);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit" }).format(date);
  }
  const days = (now - date) / 86400000;
  if (days < 7) return new Intl.DateTimeFormat([], { weekday: "short" }).format(date);
  if (date.getFullYear() === now.getFullYear()) {
    return new Intl.DateTimeFormat([], { month: "short", day: "numeric" }).format(date);
  }
  return new Intl.DateTimeFormat([], { month: "short", day: "numeric", year: "2-digit" }).format(date);
};

const formatMessageTime = (timestamp) =>
  new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit" }).format(new Date(timestamp));

const formatDay = (timestamp) => {
  const date = new Date(timestamp);
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return new Intl.DateTimeFormat([], {
    weekday: "short", month: "long", day: "numeric", year: "numeric",
  }).format(date);
};

const bytes = (size) => {
  if (!size) return "";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(size) / Math.log(1024)));
  return `${(size / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
};

const mediaUrl = (kind, item) => {
  const version = item.content_sha256
    ? `?v=${encodeURIComponent(item.content_sha256)}` : "";
  return `/api/${kind}/${item.media_id}${version}`;
};

function toast(message) {
  els.toast.textContent = message;
  els.toast.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { els.toast.hidden = true; }, 2600);
}

function conversationCard(item) {
  const sent = item.last_direction === "outgoing" ? "You: " : "";
  return `
    <button class="conversation ${state.active?.id === item.id ? "active" : ""}" data-id="${item.id}">
      ${avatar(item)}
      <span class="conversation-copy">
        <span class="conversation-line">
          <span class="conversation-name">${escapeHtml(item.title)}</span>
          <time class="conversation-time">${formatListTime(item.last_message_ms)}</time>
        </span>
        <span class="conversation-preview">${sent}${escapeHtml(item.last_body || "")}</span>
      </span>
    </button>`;
}

async function loadConversations(reset = false) {
  if (state.loading || state.inGlobalSearch || (!reset && !state.conversationNext)) return;
  state.loading = true;
  if (reset) {
    state.conversations = [];
    state.conversationNext = null;
    els.list.innerHTML = Array.from({ length: 7 }, () => '<div class="skeleton"></div>').join("");
  }
  try {
    const params = new URLSearchParams({ limit: "50" });
    if (!reset && state.conversationNext) params.set("before", state.conversationNext);
    const data = await api(`/api/conversations?${params}`);
    state.conversations.push(...data.items);
    state.conversationNext = data.next;
    if (reset) els.list.innerHTML = state.conversations.map(conversationCard).join("");
    else els.list.insertAdjacentHTML("beforeend", data.items.map(conversationCard).join(""));
    els.loadConversations.hidden = !data.next;
  } catch (error) {
    els.list.innerHTML = `<p class="conversation-preview" style="padding:20px">${escapeHtml(error.message)}</p>`;
  } finally {
    state.loading = false;
    requestAnimationFrame(() => {
      if (!state.inGlobalSearch && state.conversationNext &&
          els.list.scrollHeight <= els.list.clientHeight + 100) loadConversations();
    });
  }
}

const musicIcon = `<svg viewBox="0 0 24 24" aria-hidden="true">
  <path d="M9 18V5l10-2v13M9 8l10-2"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/>
</svg>`;

function attachment(part) {
  // MP4 audio containers are often detected as video/mp4. The message's
  // declared audio role is authoritative for presentation and playback.
  const type = part.content_type?.startsWith("audio/")
    ? part.content_type : (part.detected_content_type || part.content_type || "");
  const name = part.filename || (part.media_id ? `Attachment ${part.media_id}` : "Attachment");
  if (!part.media_id) {
    return `<div class="attachment attachment-file attachment-missing">
      <span><svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5"/></svg>
      <strong>${escapeHtml(name)}</strong><small>Attachment unavailable</small></span>
    </div>`;
  }
  const source = mediaUrl("media", part);
  const thumbnail = mediaUrl("thumbnail", part);
  if (type.startsWith("image/")) {
    return `<button class="attachment" data-view="${source}" data-type="${escapeHtml(type)}" data-part="${part.id}">
      <img src="${thumbnail}" data-original="${source}" alt="${escapeHtml(name)}" loading="lazy" decoding="async">
    </button>`;
  }
  if (type.startsWith("video/")) {
    return `<button class="attachment" data-view="${source}" data-type="${escapeHtml(type)}" data-part="${part.id}">
      <img src="${thumbnail}" data-gallery-thumbnail alt="${escapeHtml(name)}" loading="lazy" decoding="async">
      <span class="attachment-play">▶</span>
    </button>`;
  }
  if (type.startsWith("audio/")) {
    const audioSource = mediaUrl("audio", part);
    return `<div class="attachment attachment-audio">
      <span class="audio-note">${musicIcon}</span>
      <audio controls preload="none" src="${audioSource}"></audio>
      <button class="audio-expand" data-view="${source}" data-type="${escapeHtml(type)}"
        data-part="${part.id}" aria-label="Open audio in fullscreen">↗</button>
    </div>`;
  }
  return `<button class="attachment attachment-file" data-view="${source}" data-type="${escapeHtml(type)}" data-part="${part.id}">
    <span><svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5"/></svg>
    <strong>${escapeHtml(name)}</strong><small>${escapeHtml(type || "File")} · ${bytes(part.byte_size)}</small></span>
  </button>`;
}

function reactionSymbol(reaction) {
  const values = {
    liked: "👍", loved: "❤️", disliked: "👎", laughed: "😂",
    emphasized: "‼️", questioned: "❓", photomoji: "✨",
  };
  return values[String(reaction).toLowerCase()] || reaction;
}

function stickerImage(sticker, placement = "standalone") {
  const name = sticker.short_description || sticker.filename || "Sticker";
  const actor = sticker.reactor_name || sticker.display_address;
  const title = actor ? `${actor}: ${name}` : name;
  const image = `<img src="${mediaUrl("sticker", sticker)}" alt="${escapeHtml(name)}"
    title="${escapeHtml(title)}" loading="lazy" decoding="async">`;
  if (placement === "standalone" && sticker.part_id) {
    return `<button class="sticker sticker-standalone" data-view="${mediaUrl("media", sticker)}"
      data-type="${escapeHtml(sticker.content_type || "image/webp")}" data-part="${sticker.part_id}">
      ${image}</button>`;
  }
  if (placement === "placed") {
    const x = Number.isFinite(Number(sticker.position_x)) ? Number(sticker.position_x) : .5;
    const y = Number.isFinite(Number(sticker.position_y)) ? Number(sticker.position_y) : .1;
    const sourceScale = Number.isFinite(Number(sticker.scale)) ? Number(sticker.scale) : .367;
    const scale = Math.max(.35, Math.min(2.5, sourceScale / .367));
    const rotation = Number.isFinite(Number(sticker.rotation)) ? Number(sticker.rotation) : 0;
    return `<span class="sticker sticker-placed" style="--sticker-x:${x};--sticker-y:${y};--sticker-scale:${scale};--sticker-rotation:${rotation}">
      ${image}</span>`;
  }
  return `<span class="sticker sticker-back">${image}</span>`;
}

function appMessageCard(card) {
  if (!card) return "";
  const content = `<span class="app-message-name">${escapeHtml(card.app_name || "Messages app")}</span>
    <strong>${escapeHtml(card.title || "App message")}</strong>
    ${card.subtitle && card.subtitle !== card.title
      ? `<small>${escapeHtml(card.subtitle)}</small>` : ""}`;
  return card.url
    ? `<a class="app-message-card" href="${escapeHtml(card.url)}" target="_blank" rel="noopener">${content}</a>`
    : `<div class="app-message-card">${content}</div>`;
}

function effectLabel(effectId) {
  const suffix = String(effectId || "").split(".").at(-1)
    .replace(/^CK/, "").replace(/Effect$/, "");
  const labels = {
    invisibleink: "Invisible Ink", gentle: "Gentle", impact: "Slam", loud: "Loud",
    HappyBirthday: "Balloons", Confetti: "Confetti", Spotlight: "Spotlight",
    Sparkles: "Celebration", Lasers: "Lasers", Heart: "Heart",
    Fireworks: "Fireworks", Echo: "Echo",
  };
  return labels[suffix] || suffix.replace(/([a-z])([A-Z])/g, "$1 $2") || "message effect";
}

function editHistory(message) {
  if (!message.edit_history?.length) return "";
  return `<details class="edit-history"><summary>Edited · show history</summary>
    ${message.edit_history.map((edit) => `<div><time>${formatMessageTime(edit.timestamp_ms)}</time>
      <span>${escapeHtml(edit.body || "Unsent part")}</span></div>`).join("")}
  </details>`;
}

function renderMessages(items, prepend = false, append = false) {
  const visible = items.filter((message) =>
    !message.reaction_fallback_target_id && message.status !== "hidden"
  );
  let previousDay = null;
  let previousTime = null;
  const html = visible.map((message) => {
    const day = new Date(message.timestamp_ms).toDateString();
    const divider = day !== previousDay
      ? `<div class="day-divider">${formatDay(message.timestamp_ms)}</div>` : "";
    const gap = previousTime && message.timestamp_ms - previousTime > 20 * 60 * 1000;
    previousDay = day;
    previousTime = message.timestamp_ms;
    const mediaParts = message.parts.filter((part) => !part.is_sticker && (
      part.media_id || (part.filename && !String(part.content_type || "").startsWith("text/"))
    )
    );
    const standaloneStickers = (message.stickers || []).filter((item) => item.kind === "standalone");
    const placedStickers = (message.stickers || []).filter((item) =>
      item.kind === "placed" && item.position_x != null && item.position_y != null
    );
    const stickerbacks = (message.stickers || []).filter((item) =>
      item.kind === "stickerback" ||
      (item.kind === "placed" && (item.position_x == null || item.position_y == null))
    );
    const inlineText = message.parts
      .filter((part) => !part.media_id && part.text_content && part.text_content !== message.body)
      .map((part) => part.text_content).join("\n");
    const shownBody = message.app_message &&
      (message.status === "app" || message.app_message.url === message.body)
      ? "" : (message.body || inlineText);
    if (message.status === "announcement") {
      return `${divider}<article class="message-row announcement ${gap ? "gap" : ""}" id="message-${message.id}">
        <div class="announcement-text">${escapeHtml(message.body || "Conversation update")}</div>
        <div class="message-meta">${formatMessageTime(message.timestamp_ms)}</div>
      </article>`;
    }
    const reply = message.reply?.target_message_id
      ? `<button class="reply-preview" data-reply="${message.reply.target_message_id}">
          <span>Replying to</span>${escapeHtml(message.reply.target_body || "Attachment")}</button>` : "";
    const stickerOnly = standaloneStickers.length && !reply && !message.subject &&
      !mediaParts.length && !shownBody && !message.app_message;
    return `${divider}<article class="message-row ${message.direction} ${escapeHtml(message.transport || "")} ${gap ? "gap" : ""} ${stickerOnly ? "sticker-only" : ""}" id="message-${message.id}">
      <div class="bubble-stage">
        <div class="bubble">
          ${reply}
          ${message.subject ? `<div class="bubble-subject">${escapeHtml(message.subject)}</div>` : ""}
          ${appMessageCard(message.app_message)}
          ${standaloneStickers.length ? `<div class="standalone-stickers">${standaloneStickers.map((item) => stickerImage(item)).join("")}</div>` : ""}
          ${mediaParts.length ? `<div class="attachment-grid ${mediaParts.length === 1 ? "single" : ""}">
            ${mediaParts.map(attachment).join("")}</div>` : ""}
          ${shownBody ? `<div class="bubble-text">${escapeHtml(shownBody)}</div>` : ""}
          ${message.effect_id ? `<div class="message-effect">Sent with ${escapeHtml(effectLabel(message.effect_id))}</div>` : ""}
          ${editHistory(message)}
        </div>
        ${placedStickers.map((item) => stickerImage(item, "placed")).join("")}
      </div>
      ${stickerbacks.length ? `<div class="stickerbacks">${stickerbacks.map((item) => stickerImage(item, "back")).join("")}</div>` : ""}
      ${message.reactions?.length ? `<div class="reactions" title="${escapeHtml(message.reactions.map((r) => r.reactor_name || r.display_address || "").join(", "))}">
        ${message.reactions.map((reaction) => `<span>${escapeHtml(reactionSymbol(reaction.reaction))}</span>`).join("")}</div>` : ""}
      <div class="message-meta">${formatMessageTime(message.timestamp_ms)} · ${escapeHtml(message.transport.toUpperCase())}${message.status === "edited" ? " · Edited" : ""}</div>
    </article>`;
  }).join("");
  if (prepend) els.messages.insertAdjacentHTML("afterbegin", html);
  else if (append) els.messages.insertAdjacentHTML("beforeend", html);
  else els.messages.innerHTML = html;
}

async function focusMessageInThread(messageId) {
  const node = document.querySelector(`#message-${messageId}`);
  if (!node) return;
  node.classList.add("search-target");
  node.scrollIntoView({ block: "center" });
  const pendingImages = [...node.querySelectorAll("img")].filter(
    (image) => !image.complete
  );
  if (pendingImages.length) {
    await Promise.race([
      Promise.all(pendingImages.map((image) => new Promise((resolve) => {
        image.addEventListener("load", resolve, { once: true });
        image.addEventListener("error", resolve, { once: true });
      }))),
      new Promise((resolve) => setTimeout(resolve, 4000)),
    ]);
  }
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  node.scrollIntoView({ block: "center" });
}

async function openConversation(id, focusMessage = null) {
  try {
    const [detail, data] = await Promise.all([
      api(`/api/conversations/${id}`),
      api(`/api/conversations/${id}/messages?limit=60${focusMessage ? `&around=${focusMessage}` : ""}`),
    ]);
    state.active = detail;
    state.messageNext = data.next;
    state.messageNewer = data.newer;
    document.body.classList.add("thread-open");
    els.thread.classList.remove("empty");
    els.welcome.hidden = true;
    els.person.innerHTML = `${avatar(detail, true)}<span class="thread-title">
      <strong>${escapeHtml(detail.title)}</strong>
      <span>${detail.message_count.toLocaleString()} messages${detail.subtitle ? ` · ${escapeHtml(detail.subtitle)}` : ""}</span>
    </span>`;
    renderMessages(data.items);
    els.loadOlder.hidden = !data.next;
    els.list.querySelectorAll(".conversation").forEach((node) =>
      node.classList.toggle("active", Number(node.dataset.id) === id)
    );
    history.replaceState(null, "", `#conversation=${id}`);
    await new Promise((resolve) => requestAnimationFrame(resolve));
    if (focusMessage) {
      await focusMessageInThread(focusMessage);
    } else {
      requestAnimationFrame(() => {
        els.scroll.scrollTop = els.scroll.scrollHeight;
        if (state.messageNext &&
            els.scroll.scrollHeight <= els.scroll.clientHeight + 100) loadOlder();
      });
    }
  } catch (error) {
    toast(error.message);
  }
}

async function loadOlder() {
  if (!state.active || !state.messageNext || state.messageLoading) return;
  state.messageLoading = true;
  const oldHeight = els.scroll.scrollHeight;
  const oldTop = els.scroll.scrollTop;
  try {
    const params = new URLSearchParams({
      limit: "60",
      beforeMs: state.messageNext.beforeMs,
      beforeId: state.messageNext.beforeId,
    });
    const data = await api(`/api/conversations/${state.active.id}/messages?${params}`);
    renderMessages(data.items, true);
    state.messageNext = data.next;
    els.loadOlder.hidden = !data.next;
    els.scroll.scrollTop = oldTop + els.scroll.scrollHeight - oldHeight;
  } catch (error) {
    toast(error.message);
  } finally {
    state.messageLoading = false;
  }
}

async function loadNewer() {
  if (!state.active || !state.messageNewer || state.messageLoading) return;
  state.messageLoading = true;
  try {
    const params = new URLSearchParams({
      limit: "60",
      afterMs: state.messageNewer.afterMs,
      afterId: state.messageNewer.afterId,
    });
    const data = await api(`/api/conversations/${state.active.id}/messages?${params}`);
    renderMessages(data.items, false, true);
    state.messageNewer = data.newer;
  } catch (error) {
    toast(error.message);
  } finally {
    state.messageLoading = false;
  }
}

async function globalSearch(query) {
  if (!query.trim()) return closeSearch();
  state.inGlobalSearch = true;
  els.filterRow.hidden = false;
  els.list.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    const grouped = data.conversations.map((item) => `
      <button class="grouped-result" data-conversation="${item.conversation_id}"
              data-message="${item.latest_message_id}" data-query="${escapeHtml(query)}">
        ${avatar(item)}
        <span class="grouped-result-copy">
          <span class="grouped-result-head">
            <strong>${escapeHtml(item.title)}</strong>
            <span class="match-count">${item.occurrences.toLocaleString()} ${item.occurrences === 1 ? "match" : "matches"}</span>
          </span>
          <p>${escapeHtml(item.snippet).replaceAll("&lt;mark&gt;", "<mark>").replaceAll("&lt;/mark&gt;", "</mark>")}</p>
          <time>Latest · ${formatDay(item.timestamp_ms)} at ${formatMessageTime(item.timestamp_ms)}</time>
        </span>
      </button>`).join("");
    els.list.innerHTML = data.total_matches
      ? `<div class="search-summary">${data.total_matches.toLocaleString()} occurrences across ${data.conversation_count.toLocaleString()} conversations</div>${grouped}`
      : '<p class="conversation-preview" style="padding:20px">No matching messages</p>';
    els.loadConversations.hidden = true;
  } catch (error) {
    els.list.innerHTML = `<p class="conversation-preview" style="padding:20px">${escapeHtml(error.message)}</p>`;
  }
}

function closeSearch() {
  state.inGlobalSearch = false;
  els.searchInput.value = "";
  els.filterRow.hidden = true;
  loadConversations(true);
}

function updateMatchNav() {
  const active = state.threadMatchIndex >= 0;
  els.matchNav.hidden = !active;
  if (!active) return;
  els.matchStatus.textContent =
    `${state.threadMatchIndex + 1} of ${state.threadMatchTotal.toLocaleString()}`;
  els.newerMatch.disabled = state.threadMatchIndex <= 0;
  els.olderMatch.disabled =
    state.threadMatchIndex >= state.threadMatchTotal - 1;
}

async function loadThreadMatchPage(query, offset = 0) {
  const data = await api(
    `/api/conversations/${state.active.id}/search?q=${encodeURIComponent(query)}&limit=100&offset=${offset}`
  );
  if (!offset) state.threadMatches = data.items;
  else state.threadMatches.push(...data.items);
  state.threadMatchTotal = data.total;
  state.threadMatchNext = data.next_offset;
  return data;
}

async function jumpToMatch(index) {
  if (index < 0 || index >= state.threadMatchTotal) return;
  if (index >= state.threadMatches.length && state.threadMatchNext != null) {
    await loadThreadMatchPage(els.threadSearchInput.value, state.threadMatchNext);
  }
  const match = state.threadMatches[index];
  if (!match) return;
  state.threadMatchIndex = index;
  updateMatchNav();
  await openConversation(state.active.id, match.id);
  requestAnimationFrame(() => {
    const node = document.querySelector(`#message-${match.id}`);
    node?.classList.add("search-target");
    node?.scrollIntoView({ block: "center" });
  });
}

async function threadSearch(query, initialMessage = null) {
  if (!query.trim() || !state.active) return;
  try {
    const data = await loadThreadMatchPage(query);
    if (!data.total) {
      state.threadMatchIndex = -1;
      updateMatchNav();
      return toast("No matches in this conversation");
    }
    const index = initialMessage
      ? Math.max(0, state.threadMatches.findIndex((item) => item.id === initialMessage))
      : 0;
    await jumpToMatch(index);
  } catch (error) {
    toast(error.message);
  }
}

async function openGroupedResult(conversationId, messageId, query) {
  await openConversation(conversationId, messageId);
  els.threadSearch.hidden = false;
  els.threadSearchInput.value = query;
  await threadSearch(query, messageId);
}

function galleryItem(item) {
  const source = item.category === "audio"
    ? mediaUrl("audio", item) : mediaUrl("media", item);
  const type = item.detected_content_type || item.content_type || "";
  const name = item.filename || `${item.category} attachment`;
  let preview;
  if (item.category === "image") {
    preview = `<img data-thumbnail="${mediaUrl("thumbnail", item)}" data-gallery-thumbnail alt="${escapeHtml(name)}" decoding="async">`;
  } else if (item.category === "video") {
    preview = `<img data-thumbnail="${mediaUrl("thumbnail", item)}" data-gallery-thumbnail alt="${escapeHtml(name)}" decoding="async"><span class="gallery-play">▶</span>`;
  } else if (item.category === "audio") {
    preview = `<span class="gallery-audio-icon">${musicIcon}</span>`;
  } else {
    preview = `<span class="gallery-item-file"><strong>${escapeHtml(name)}</strong><small>${bytes(item.byte_size)}</small></span>`;
  }
  return `<button class="gallery-item" data-view="${source}" data-type="${escapeHtml(type)}"
      data-conversation="${item.conversation_id}" data-message="${item.message_id}"
      data-part="${item.part_id}">
    ${preview}
    <span class="gallery-caption">${escapeHtml(item.conversation_title)} · ${formatListTime(item.timestamp_ms)}</span>
  </button>`;
}

const GALLERY_WINDOW_ITEMS = 360;

function galleryGeometry() {
  const width = Math.max(1, els.galleryGrid.clientWidth);
  const columns = innerWidth <= 390 ? 2
    : innerWidth <= 760 ? 3
      : Math.max(1, Math.floor((width + 7) / 162));
  const gap = innerWidth <= 760 ? 3 : 7;
  const itemWidth = (width - gap * (columns - 1)) / columns;
  return { columns, pitch: itemWidth + gap };
}

function renderGalleryWindow(anchorIndex = null) {
  galleryMediaObserver?.disconnect();
  const gallery = state.gallery;
  const { columns, pitch } = galleryGeometry();
  const maxStart = Math.max(
    0, Math.floor(Math.max(0, gallery.items.length - GALLERY_WINDOW_ITEMS) / columns) * columns,
  );
  if (anchorIndex != null) {
    const buffered = Math.max(0, anchorIndex - Math.floor(GALLERY_WINDOW_ITEMS / 3));
    gallery.windowStart = Math.min(maxStart, Math.floor(buffered / columns) * columns);
  } else {
    gallery.windowStart = Math.min(gallery.windowStart, maxStart);
  }
  const end = Math.min(gallery.items.length, gallery.windowStart + GALLERY_WINDOW_ITEMS);
  const topRows = Math.floor(gallery.windowStart / columns);
  const bottomRows = Math.ceil((gallery.items.length - end) / columns);
  els.galleryGrid.innerHTML = `
    <div class="gallery-spacer" style="height:${topRows * pitch}px"></div>
    <div class="gallery-window">${gallery.items.slice(gallery.windowStart, end).map(galleryItem).join("")}</div>
    <div class="gallery-spacer" style="height:${bottomRows * pitch}px"></div>`;
  observeGalleryMedia();
}

function updateGalleryWindow() {
  const gallery = state.gallery;
  if (!gallery.items.length) return;
  const { columns, pitch } = galleryGeometry();
  const visibleIndex = Math.max(0, Math.floor(els.galleryScroll.scrollTop / pitch) * columns);
  const windowEnd = gallery.windowStart + GALLERY_WINDOW_ITEMS;
  if (visibleIndex < gallery.windowStart + columns * 4 ||
      visibleIndex > windowEnd - columns * 8) {
    renderGalleryWindow(visibleIndex);
  }
}

const galleryMediaObserver = "IntersectionObserver" in window
  ? new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const media = entry.target;
        if (entry.isIntersecting) {
          if (media.dataset.thumbnail && !media.getAttribute("src")) {
            media.src = media.dataset.thumbnail;
          } else if (media.dataset.video && !media.getAttribute("src")) {
            media.preload = "metadata";
            media.src = media.dataset.video;
          }
        } else if (media.getAttribute("src")) {
          media.removeAttribute("src");
          if (media instanceof HTMLVideoElement) {
            media.preload = "none";
            media.load();
          }
        }
      }
    }, { root: els.galleryScroll, rootMargin: "700px 0px" })
  : null;

function observeGalleryMedia() {
  for (const media of els.galleryGrid.querySelectorAll("[data-thumbnail],[data-video]")) {
    if (galleryMediaObserver) galleryMediaObserver.observe(media);
    else {
      media.src = media.dataset.thumbnail || media.dataset.video;
    }
  }
}

async function loadGallery(reset = false) {
  const gallery = state.gallery;
  if (gallery.loading || (!reset && !gallery.next)) return;
  gallery.loading = true;
  els.galleryLoading.hidden = false;
  if (reset) {
    galleryMediaObserver?.disconnect();
    gallery.items = [];
    gallery.next = null;
    gallery.windowStart = 0;
    renderGalleryWindow(0);
  }
  try {
    const params = new URLSearchParams({ type: gallery.type, limit: "60" });
    if (gallery.conversation) params.set("conversation", gallery.conversation);
    if (!reset && gallery.next) {
      params.set("beforeMs", gallery.next.beforeMs);
      params.set("beforePart", gallery.next.beforePart);
    }
    const data = await api(`/api/attachments?${params}`);
    const previousCount = gallery.items.length;
    gallery.items.push(...data.items);
    gallery.next = data.next;
    renderGalleryWindow(reset ? 0 : previousCount);
  } catch (error) {
    toast(error.message);
  } finally {
    gallery.loading = false;
    els.galleryLoading.hidden = true;
    requestAnimationFrame(() => {
      if (gallery.next &&
          els.galleryScroll.scrollHeight <= els.galleryScroll.clientHeight + 100) {
        loadGallery();
      }
    });
  }
}

function openGallery(conversation = null) {
  state.gallery.conversation = conversation?.id || null;
  state.gallery.type = "all";
  els.galleryTitle.textContent = conversation
    ? `${conversation.title} attachments` : "All attachments";
  els.galleryFilters.querySelectorAll("button").forEach((button) =>
    button.classList.toggle("active", button.dataset.type === "all")
  );
  els.gallery.hidden = false;
  els.galleryScroll.scrollTop = 0;
  loadGallery(true);
}

const attachmentType = (item) => {
  if (item.category === "audio") return item.content_type || "audio/mp4";
  return item.detected_content_type || item.content_type || "application/octet-stream";
};

async function attachmentCatalog(conversation, type) {
  const params = new URLSearchParams({ catalog: "1", type: type || "all" });
  if (conversation) params.set("conversation", conversation);
  return (await api(`/api/attachments?${params}`)).items;
}

function renderViewer() {
  const item = state.viewer.items[state.viewer.index];
  if (!item) return;
  const source = mediaUrl("media", item);
  const type = attachmentType(item);
  const name = item.filename || `${item.category || "File"} attachment`;
  if (type.startsWith("video/")) {
    els.viewerContent.innerHTML = `<video src="${source}" controls autoplay playsinline></video>`;
  } else if (type.startsWith("audio/")) {
    const audioSource = mediaUrl("audio", item);
    els.viewerContent.innerHTML = `<div class="viewer-audio">
      <span class="audio-note">${musicIcon}</span>
      <audio src="${audioSource}" controls autoplay></audio>
    </div>`;
  } else if (type.startsWith("image/")) {
    const needsPreview = ["image/heic", "image/heif", "image/heic-sequence", "image/avif"]
      .includes(type.toLowerCase());
    const display = item.is_sticker ? mediaUrl("sticker", item)
      : (needsPreview ? mediaUrl("preview", item) : source);
    const fallback = needsPreview ? ` data-original="${mediaUrl("thumbnail", item)}"` : "";
    els.viewerContent.innerHTML = `<img src="${display}"${fallback} alt="${escapeHtml(name)}">`;
  } else if (type === "application/pdf") {
    els.viewerContent.innerHTML = `<iframe src="${source}" title="${escapeHtml(name)}"></iframe>`;
  } else {
    els.viewerContent.innerHTML = `<div class="viewer-file">
      <strong>${escapeHtml(name)}</strong>
      <p>${escapeHtml(type)} · ${bytes(item.byte_size)}</p>
      <a href="${source}" target="_blank" rel="noopener">Open file</a>
    </div>`;
  }
  els.viewerCounter.textContent =
    `${(state.viewer.index + 1).toLocaleString()} of ${state.viewer.items.length.toLocaleString()}`;
  els.viewerName.textContent = name;
  els.viewerMeta.textContent =
    `${item.conversation_title} · ${formatDay(item.timestamp_ms)} at ${formatMessageTime(item.timestamp_ms)}`;
  els.previousAttachment.disabled = state.viewer.index <= 0;
  els.nextAttachment.disabled = state.viewer.index >= state.viewer.items.length - 1 &&
    (els.gallery.hidden || !state.gallery.next);
}

async function openViewerForPart(partId, conversation, type = "all") {
  els.viewer.hidden = false;
  els.viewerContent.innerHTML = '<div class="gallery-loading">Loading attachment…</div>';
  try {
    const galleryMatches = !els.gallery.hidden &&
      conversation === state.gallery.conversation && type === state.gallery.type;
    const items = galleryMatches
      ? state.gallery.items
      : await attachmentCatalog(conversation, type);
    const index = items.findIndex((item) => item.part_id === Number(partId));
    if (index < 0) throw new Error("Attachment was not found in this view");
    state.viewer.items = items;
    state.viewer.index = index;
    renderViewer();
  } catch (error) {
    els.viewer.hidden = true;
    toast(error.message);
  }
}

async function moveViewer(direction) {
  if (direction > 0 && state.viewer.index + 1 >= state.viewer.items.length &&
      !els.gallery.hidden && state.gallery.next) {
    await loadGallery();
    state.viewer.items = state.gallery.items;
  }
  const next = state.viewer.index + direction;
  if (next < 0 || next >= state.viewer.items.length) return;
  state.viewer.index = next;
  renderViewer();
}

async function showViewerMessage() {
  const item = state.viewer.items[state.viewer.index];
  if (!item) return;
  els.viewer.hidden = true;
  els.gallery.hidden = true;
  els.viewerContent.innerHTML = "";
  await openConversation(item.conversation_id, item.message_id);
}

els.list.addEventListener("click", (event) => {
  const conversation = event.target.closest(".conversation");
  if (conversation) openConversation(Number(conversation.dataset.id));
  const hit = event.target.closest(".search-hit");
  if (hit) openConversation(Number(hit.dataset.conversation), Number(hit.dataset.message));
  const grouped = event.target.closest(".grouped-result");
  if (grouped) openGroupedResult(
    Number(grouped.dataset.conversation),
    Number(grouped.dataset.message),
    grouped.dataset.query,
  );
});
els.messages.addEventListener("click", (event) => {
  const reply = event.target.closest("[data-reply]");
  if (reply && state.active) {
    openConversation(state.active.id, Number(reply.dataset.reply));
    return;
  }
  const target = event.target.closest("[data-view]");
  if (target) openViewerForPart(target.dataset.part, state.active?.id, "all");
});
els.loadConversations.addEventListener("click", () => loadConversations());
els.loadOlder.addEventListener("click", loadOlder);
els.list.addEventListener("scroll", () => {
  if (els.list.scrollHeight - els.list.scrollTop - els.list.clientHeight < 350) {
    loadConversations();
  }
}, { passive: true });
els.scroll.addEventListener("scroll", () => {
  if (els.scroll.scrollTop < 260) loadOlder();
  const distance = els.scroll.scrollHeight - els.scroll.scrollTop - els.scroll.clientHeight;
  if (distance < 260) loadNewer();
}, { passive: true });
els.back.addEventListener("click", () => {
  document.body.classList.remove("thread-open");
  history.replaceState(null, "", location.pathname);
});
els.searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  globalSearch(els.searchInput.value);
});
els.searchInput.addEventListener("input", () => {
  els.clearSearch.style.visibility = els.searchInput.value ? "visible" : "hidden";
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => {
    if (els.searchInput.value.trim().length >= 2) globalSearch(els.searchInput.value);
    else if (!els.searchInput.value) closeSearch();
  }, 350);
});
els.clearSearch.addEventListener("click", closeSearch);
els.closeResults.addEventListener("click", closeSearch);
els.threadSearchButton.addEventListener("click", () => {
  els.threadSearch.hidden = false;
  els.threadSearchInput.focus();
});
els.closeThreadSearch.addEventListener("click", () => {
  els.threadSearch.hidden = true;
  els.threadSearchInput.value = "";
  state.threadMatchIndex = -1;
  state.threadMatches = [];
  updateMatchNav();
});
els.threadSearchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") threadSearch(event.target.value);
});
els.newerMatch.addEventListener("click", () => jumpToMatch(state.threadMatchIndex - 1));
els.olderMatch.addEventListener("click", () => jumpToMatch(state.threadMatchIndex + 1));
els.globalAttachments.addEventListener("click", () => openGallery());
els.threadAttachments.addEventListener("click", () => {
  if (state.active) openGallery(state.active);
});
els.closeGallery.addEventListener("click", () => { els.gallery.hidden = true; });
els.galleryFilters.addEventListener("click", (event) => {
  const button = event.target.closest("[data-type]");
  if (!button) return;
  state.gallery.type = button.dataset.type;
  els.galleryFilters.querySelectorAll("button").forEach((item) =>
    item.classList.toggle("active", item === button)
  );
  els.galleryScroll.scrollTop = 0;
  loadGallery(true);
});
let galleryScrollFrame = null;
els.galleryScroll.addEventListener("scroll", () => {
  if (galleryScrollFrame != null) return;
  galleryScrollFrame = requestAnimationFrame(() => {
    galleryScrollFrame = null;
    updateGalleryWindow();
    if (els.galleryScroll.scrollHeight - els.galleryScroll.scrollTop -
        els.galleryScroll.clientHeight < 500) loadGallery();
  });
}, { passive: true });
window.addEventListener("resize", () => {
  if (!els.gallery.hidden) updateGalleryWindow();
});
els.galleryGrid.addEventListener("click", (event) => {
  const item = event.target.closest("[data-view]");
  if (item) openViewerForPart(
    item.dataset.part, state.gallery.conversation, state.gallery.type
  );
});
els.closeViewer.addEventListener("click", () => {
  els.viewer.hidden = true;
  els.viewerContent.innerHTML = "";
});
els.previousAttachment.addEventListener("click", () => moveViewer(-1));
els.nextAttachment.addEventListener("click", () => moveViewer(1));
els.showInConversation.addEventListener("click", showViewerMessage);
els.viewerContent.addEventListener("touchstart", (event) => {
  state.viewer.touchStartX = event.changedTouches[0]?.clientX ?? null;
}, { passive: true });
els.viewerContent.addEventListener("touchend", (event) => {
  if (state.viewer.touchStartX == null) return;
  const distance = (event.changedTouches[0]?.clientX ?? state.viewer.touchStartX)
    - state.viewer.touchStartX;
  if (Math.abs(distance) > 45) moveViewer(distance < 0 ? 1 : -1);
  state.viewer.touchStartX = null;
}, { passive: true });
document.addEventListener("keydown", (event) => {
  if (els.viewer.hidden) return;
  if (event.key === "ArrowLeft") moveViewer(-1);
  else if (event.key === "ArrowRight") moveViewer(1);
  else if (event.key === "Escape") els.closeViewer.click();
});
document.addEventListener("error", (event) => {
  const image = event.target;
  if (image instanceof HTMLImageElement && image.dataset.galleryThumbnail != null) {
    image.classList.add("thumbnail-unavailable");
    image.removeAttribute("src");
    return;
  }
  if (image instanceof HTMLImageElement && image.dataset.original &&
      image.src !== new URL(image.dataset.original, location.href).href) {
    image.src = image.dataset.original;
  }
}, true);
els.viewer.addEventListener("click", (event) => {
  if (event.target === els.viewer) els.closeViewer.click();
});
els.theme.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("textview-theme", next);
});

const savedTheme = localStorage.getItem("textview-theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
else if (matchMedia("(prefers-color-scheme: dark)").matches) {
  document.documentElement.dataset.theme = "dark";
}
els.clearSearch.style.visibility = "hidden";
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js");
function openHashRoute() {
  const id = Number(location.hash.match(/conversation=(\d+)/)?.[1]);
  const message = Number(location.hash.match(/message=(\d+)/)?.[1]);
  if (id && (state.active?.id !== id || message)) openConversation(id, message || null);
}
window.addEventListener("hashchange", openHashRoute);
loadConversations(true).then(() => {
  const params = new URLSearchParams(location.search);
  if (location.hash) openHashRoute();
  else if (params.get("search")) {
    els.searchInput.value = params.get("search");
    els.clearSearch.style.visibility = "visible";
    globalSearch(params.get("search"));
  } else if (params.get("attachments") != null) {
    openGallery();
  }
});
