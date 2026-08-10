(() => {
  const LEGACY_CHAT_DELAY_KEY = "chzzkbackup-chat-delay-seconds";
  const CHAT_DELAY_MIN = -60;
  const CHAT_DELAY_MAX = 60;
  const CHAT_DELAY_STEP = 0.5;

  const normalizedSeconds = value => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : 0;
  };
  const normalizeChatDelay = value => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return 0;
    const stepped = Math.round(parsed / CHAT_DELAY_STEP) * CHAT_DELAY_STEP;
    return Math.min(CHAT_DELAY_MAX, Math.max(CHAT_DELAY_MIN, stepped));
  };
  const adjustedChatOffset = (offset, delay, duration) => {
    if (offset == null || offset === "") return null;
    const parsedOffset = Number(offset);
    if (!Number.isFinite(parsedOffset)) return null;
    const adjusted = Math.max(0, parsedOffset + normalizeChatDelay(delay));
    const parsedDuration = duration == null ? Number.NaN : Number(duration);
    return Number.isFinite(parsedDuration) && parsedDuration >= 0
      ? Math.min(adjusted, parsedDuration)
      : adjusted;
  };
  const pad2 = value => String(value).padStart(2, "0");
  const formatTimelineTime = value => {
    const total = normalizedSeconds(value);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return hours > 0
      ? `${hours}:${pad2(minutes)}:${pad2(seconds)}`
      : `${pad2(minutes)}:${pad2(seconds)}`;
  };
  const formatDelay = value => {
    const normalized = normalizeChatDelay(value);
    return `${normalized >= 0 ? "+" : ""}${normalized.toFixed(1)}초`;
  };
  const formatFilenameTime = value => {
    const total = normalizedSeconds(value);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return `${pad2(hours)}-${pad2(minutes)}-${pad2(seconds)}`;
  };
  const sanitizeFilenameBase = value => {
    const cleaned = String(value || "")
      .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
      .trim()
      .replace(/[. ]+$/g, "");
    return Array.from(cleaned).slice(0, 100).join("").replace(/[. ]+$/g, "") || "video";
  };
  const screenshotFilename = (title, currentTime) =>
    `${sanitizeFilenameBase(title)}_${formatFilenameTime(currentTime)}.png`;
  const clampedSeekTime = (currentTime, delta, duration) => {
    const current = Number(currentTime);
    const change = Number(delta);
    const maximum = Number(duration);
    const target = Math.max(0, (Number.isFinite(current) ? current : 0) + (Number.isFinite(change) ? change : 0));
    return Number.isFinite(maximum) && maximum >= 0 ? Math.min(maximum, target) : target;
  };
  const chatScrollTarget = (
    scrollTop,
    listTop,
    listHeight,
    rowTop,
    rowHeight,
    scrollHeight
  ) => {
    const viewportHeight = Math.max(0, Number(listHeight) || 0);
    const maximum = Math.max(0, (Number(scrollHeight) || 0) - viewportHeight);
    const target = (Number(scrollTop) || 0)
      + ((Number(rowTop) || 0) - (Number(listTop) || 0))
      - (viewportHeight - Math.max(0, Number(rowHeight) || 0)) / 2;
    return Math.min(maximum, Math.max(0, target));
  };
  const shouldHandleSeekShortcut = event => {
    if (
      !["ArrowLeft", "ArrowRight"].includes(String(event.key || "")) ||
      event.altKey || event.ctrlKey || event.metaKey || event.repeat
    ) return false;
    const target = event.target || {};
    const tag = String(target.tagName || "").toLowerCase();
    return !["input", "textarea", "select", "button"].includes(tag)
      && !target.isContentEditable;
  };
  const seekStepForEvent = event => (event.shiftKey ? 10 : 5)
    * (event.key === "ArrowLeft" ? -1 : 1);

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      adjustedChatOffset,
      formatDelay,
      formatTimelineTime,
      normalizeChatDelay,
      screenshotFilename,
      chatScrollTarget,
      clampedSeekTime,
      seekStepForEvent,
      shouldHandleSeekShortcut,
    };
  }
  if (typeof document === "undefined") return;

  const player = document.getElementById("player");
  const list = document.getElementById("chat-list");
  const search = document.getElementById("chat-search");
  const autoScroll = document.getElementById("auto-scroll");
  const filters = document.getElementById("chat-types");
  const clipStart = document.getElementById("clip-start");
  const clipEnd = document.getElementById("clip-end");
  const captureButton = document.getElementById("capture-frame");
  const captureStatus = document.getElementById("capture-status");
  const delayEarlier = document.getElementById("chat-delay-earlier");
  const delayLater = document.getElementById("chat-delay-later");
  const delayReset = document.getElementById("chat-delay-reset");
  const delayValue = document.getElementById("chat-delay-value");
  let rows = [], enabled = new Set(), visible = [], active = -1;
  const mediaSync = window.CHZZK_MEDIA_SYNC || {};
  let chatDelay = normalizeChatDelay(mediaSync.chat_delay_seconds);
  const channelDefaultChatDelay = normalizeChatDelay(
    mediaSync.channel_default_chat_delay_seconds
  );
  let chatLoaded = false;
  let captureStatusTimer;

  const escapeHtml = text => {
    const node = document.createElement("span");
    node.textContent = text;
    return node.innerHTML;
  };
  const currentVideoDuration = () =>
    Number.isFinite(player.duration) && player.duration >= 0 ? player.duration : undefined;
  const effectiveOffset = row =>
    adjustedChatOffset(row.offset_seconds, chatDelay, currentVideoDuration());
  const syncChatToPlayer = () => {
    let found = -1;
    for (let index = 0; index < visible.length; index += 1) {
      const offset = effectiveOffset(visible[index]);
      if (offset != null && offset <= player.currentTime) found = index;
      else if (offset != null) break;
    }
    if (found === active && list.querySelector(".current")) return;
    list.querySelector(".current")?.classList.remove("current");
    active = found;
    const element = list.querySelector(`[data-index='${found}']`);
    if (element) {
      element.classList.add("current");
      if (autoScroll.checked) {
        const listRect = list.getBoundingClientRect();
        const rowRect = element.getBoundingClientRect();
        const top = chatScrollTarget(
          list.scrollTop,
          listRect.top,
          list.clientHeight,
          rowRect.top,
          rowRect.height,
          list.scrollHeight
        );
        list.scrollTo({ top, behavior: "auto" });
      }
    }
  };
  const render = () => {
    const term = search.value.trim().toLowerCase();
    visible = rows.filter(row => enabled.has(row.type) && (!term || `${row.nickname} ${row.content}`.toLowerCase().includes(term)));
    list.innerHTML = visible.length
      ? visible.map((row, index) => {
        const offset = effectiveOffset(row);
        return `<button class="chat-row ${row.sync_state}" data-index="${index}" ${offset == null ? "disabled" : ""}><time>${offset == null ? "누락" : formatTimelineTime(offset)}</time><span><strong>${escapeHtml(row.nickname)}</strong>${escapeHtml(row.content)}</span></button>`;
      }).join("")
      : '<p class="muted">표시할 채팅이 없습니다.</p>';
    active = -1;
    syncChatToPlayer();
  };
  const updateDelayControls = () => {
    delayValue.textContent = formatDelay(chatDelay);
    delayEarlier.disabled = chatDelay <= CHAT_DELAY_MIN;
    delayLater.disabled = chatDelay >= CHAT_DELAY_MAX;
    delayReset.disabled = chatDelay === channelDefaultChatDelay;
  };
  const setChatDelay = async (value, resetToChannelDefault = false) => {
    const previous = chatDelay;
    delayEarlier.disabled = true;
    delayLater.disabled = true;
    delayReset.disabled = true;
    try {
      const response = await fetch(`/media/${window.CHZZK_MEDIA_ID}/chat-delay`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(resetToChannelDefault
          ? { reset_to_channel_default: true }
          : { delay_seconds: normalizeChatDelay(value) }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "채팅 보정값 저장 실패");
      chatDelay = normalizeChatDelay(payload.chat_delay_seconds);
      if (chatLoaded) render();
    } catch (_error) {
      chatDelay = previous;
      delayValue.textContent = "저장 실패";
      window.setTimeout(updateDelayControls, 2000);
    } finally {
      updateDelayControls();
    }
  };
  try {
    localStorage.removeItem(LEGACY_CHAT_DELAY_KEY);
  } catch (_error) { /* Legacy browser value is intentionally discarded. */ }
  updateDelayControls();

  fetch(`/media/${window.CHZZK_MEDIA_ID}/chat`).then(response => response.json()).then(data => {
    rows = data;
    [...new Set(rows.map(row => row.type))].forEach(type => {
      enabled.add(type);
      filters.insertAdjacentHTML("beforeend", `<label><input type="checkbox" value="${escapeHtml(type)}" checked> ${escapeHtml(type)}</label>`);
    });
    chatLoaded = true;
    render();
  }).catch(() => {
    list.innerHTML = '<p class="error">채팅을 불러오지 못했습니다.</p>';
  });
  filters.addEventListener("change", event => {
    event.target.checked ? enabled.add(event.target.value) : enabled.delete(event.target.value);
    render();
  });
  search.addEventListener("input", render);
  list.addEventListener("click", event => {
    const button = event.target.closest("button[data-index]");
    if (!button) return;
    const offset = effectiveOffset(visible[Number(button.dataset.index)]);
    if (offset != null) {
      player.currentTime = offset;
      player.play();
    }
  });
  player.addEventListener("timeupdate", syncChatToPlayer);
  delayEarlier.addEventListener("click", () => setChatDelay(chatDelay - CHAT_DELAY_STEP));
  delayLater.addEventListener("click", () => setChatDelay(chatDelay + CHAT_DELAY_STEP));
  delayReset.addEventListener("click", () => setChatDelay(channelDefaultChatDelay, true));

  const seekBy = seconds => {
    if (player.readyState < 1) return;
    player.currentTime = clampedSeekTime(player.currentTime, seconds, player.duration);
  };
  document.querySelectorAll("[data-seek-seconds]").forEach(button => {
    button.addEventListener("click", () => seekBy(Number(button.dataset.seekSeconds)));
  });
  document.addEventListener("keydown", event => {
    if (!shouldHandleSeekShortcut(event)) return;
    event.preventDefault();
    seekBy(seekStepForEvent(event));
  });

  const formatClipTime = value => {
    const total = normalizedSeconds(value);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return `${pad2(hours)}:${pad2(minutes)}:${pad2(seconds)}`;
  };
  document.getElementById("mark-clip-start")?.addEventListener("click", () => { clipStart.value = formatClipTime(player.currentTime); });
  document.getElementById("mark-clip-end")?.addEventListener("click", () => { clipEnd.value = formatClipTime(player.currentTime); });

  const frameIsReady = () => player.readyState >= 2 && player.videoWidth > 0 && player.videoHeight > 0 && !player.seeking;
  const setCaptureStatus = (message, isError = false, clearAfter = 0) => {
    window.clearTimeout(captureStatusTimer);
    captureStatus.textContent = message;
    captureStatus.classList.toggle("error", isError);
    if (clearAfter > 0) {
      captureStatusTimer = window.setTimeout(() => {
        captureStatus.textContent = "";
        captureStatus.classList.remove("error");
      }, clearAfter);
    }
  };
  const updateCaptureAvailability = () => {
    const ready = frameIsReady();
    captureButton.disabled = !ready;
    if (ready && captureStatus.textContent === "영상 준비 중…") setCaptureStatus("");
  };
  const downloadCurrentFrame = () => {
    if (!frameIsReady()) {
      updateCaptureAvailability();
      setCaptureStatus("영상 프레임을 불러온 뒤 다시 시도하세요.", true, 4000);
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = player.videoWidth;
    canvas.height = player.videoHeight;
    const captureTime = player.currentTime;
    const filename = screenshotFilename(captureButton.dataset.mediaTitle, captureTime);
    const context = canvas.getContext("2d");
    if (!context || typeof canvas.toBlob !== "function") {
      setCaptureStatus("이 브라우저에서는 스크린샷을 저장할 수 없습니다.", true, 4000);
      return;
    }
    try {
      context.drawImage(player, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(blob => {
        if (!blob) {
          setCaptureStatus("스크린샷 생성에 실패했습니다.", true, 4000);
          return;
        }
        try {
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          link.download = filename;
          link.hidden = true;
          document.body.appendChild(link);
          link.click();
          link.remove();
          window.setTimeout(() => URL.revokeObjectURL(url), 1000);
          setCaptureStatus(`${filename} 다운로드를 시작했습니다.`, false, 4000);
        } catch (_error) {
          setCaptureStatus("스크린샷 다운로드를 시작하지 못했습니다.", true, 4000);
        }
      }, "image/png");
    } catch (_error) {
      setCaptureStatus("스크린샷 저장에 실패했습니다.", true, 4000);
    }
  };

  captureButton.addEventListener("click", downloadCurrentFrame);
  player.addEventListener("loadedmetadata", () => {
    if (chatLoaded) render();
    updateCaptureAvailability();
  });
  player.addEventListener("durationchange", () => { if (chatLoaded) render(); });
  player.addEventListener("loadeddata", updateCaptureAvailability);
  player.addEventListener("canplay", updateCaptureAvailability);
  player.addEventListener("seeked", updateCaptureAvailability);
  player.addEventListener("seeking", () => { captureButton.disabled = true; });
  player.addEventListener("emptied", () => {
    captureButton.disabled = true;
    setCaptureStatus("영상 준비 중…");
  });
  player.addEventListener("error", () => {
    captureButton.disabled = true;
    setCaptureStatus("영상을 불러오지 못해 스크린샷을 저장할 수 없습니다.", true);
  });
  document.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.repeat || event.ctrlKey || event.altKey || event.metaKey || event.shiftKey || String(event.key).toLowerCase() !== "s") return;
    const target = event.target;
    if (target instanceof Element && target.closest("input, textarea, select, button, [contenteditable]:not([contenteditable='false'])")) return;
    event.preventDefault();
    captureButton.click();
  });
  updateCaptureAvailability();
})();
