(() => {
  const SHIFT_MIN = -60;
  const SHIFT_MAX = 60;
  const SHIFT_STEP = 0.5;

  const normalizedSeconds = value => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : 0;
  };
  const pad2 = value => String(value).padStart(2, "0");
  const formatBookmarkTime = value => {
    const total = normalizedSeconds(value);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return hours > 0
      ? `${hours}:${pad2(minutes)}:${pad2(seconds)}`
      : `${pad2(minutes)}:${pad2(seconds)}`;
  };
  const parseBookmarkTime = value => {
    const text = String(value || "").trim();
    if (!/^\d+:[0-5]\d$/.test(text) && !/^\d+:[0-5]\d:[0-5]\d$/.test(text)) return null;
    const parts = text.split(":");
    const numbers = parts.map(Number);
    const seconds = numbers[numbers.length - 1];
    const minutes = numbers[numbers.length - 2];
    if (seconds > 59 || (parts.length === 3 && minutes > 59)) return null;
    return parts.length === 3
      ? numbers[0] * 3600 + minutes * 60 + seconds
      : minutes * 60 + seconds;
  };
  const normalizeBookmarkShift = value => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return 0;
    const stepped = Math.round(parsed / SHIFT_STEP) * SHIFT_STEP;
    return Math.min(SHIFT_MAX, Math.max(SHIFT_MIN, stepped));
  };
  const formatBookmarkShift = value => {
    const normalized = normalizeBookmarkShift(value);
    return `${normalized >= 0 ? "+" : ""}${normalized.toFixed(1)}초`;
  };
  const effectiveBookmarkOffset = (offset, shift, duration) => {
    const parsed = Number(offset);
    if (!Number.isFinite(parsed)) return 0;
    const adjusted = Math.max(0, parsed + normalizeBookmarkShift(shift));
    const parsedDuration = Number(duration);
    return Number.isFinite(parsedDuration) && parsedDuration >= 0
      ? Math.min(adjusted, parsedDuration)
      : adjusted;
  };
  const currentBookmarkIndex = (bookmarks, currentTime) => {
    const now = Number(currentTime);
    if (!Number.isFinite(now)) return -1;
    let current = -1;
    bookmarks.forEach((bookmark, index) => {
      if (Number(bookmark.effective_offset_seconds) <= now) current = index;
    });
    return current;
  };
  const shouldHandleBookmarkShortcut = event => {
    if (
      String(event.key || "").toLowerCase() !== "b" ||
      event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.repeat
    ) return false;
    const target = event.target || {};
    const tag = String(target.tagName || "").toLowerCase();
    return !["input", "textarea", "select", "button"].includes(tag)
      && !target.isContentEditable;
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      currentBookmarkIndex,
      effectiveBookmarkOffset,
      formatBookmarkShift,
      formatBookmarkTime,
      normalizeBookmarkShift,
      parseBookmarkTime,
      shouldHandleBookmarkShortcut,
    };
  }
  if (typeof document === "undefined") return;

  const initialized = new WeakSet();
  const statusTimers = new WeakMap();

  const apiRequest = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch (_) {
      payload = {};
    }
    if (!response.ok) {
      const detail = Array.isArray(payload.detail)
        ? payload.detail.map(item => item.msg).join(" · ")
        : payload.detail;
      throw new Error(detail || `요청에 실패했습니다. (${response.status})`);
    }
    return payload;
  };

  const setStatus = (root, message, isError = false, clearAfter = 0) => {
    const output = root.querySelector("[data-bookmark-status]");
    if (!output) return;
    window.clearTimeout(statusTimers.get(root));
    output.textContent = message;
    output.classList.toggle("error", isError);
    if (clearAfter) {
      statusTimers.set(root, window.setTimeout(() => {
        output.textContent = "";
        output.classList.remove("error");
      }, clearAfter));
    }
  };

  const button = (label, className = "") => {
    const node = document.createElement("button");
    node.type = "button";
    node.textContent = label;
    if (className) node.className = className;
    return node;
  };

  const updateShiftControls = root => {
    if (root.dataset.bookmarkScope !== "media" || !root.bookmarkCollection) return;
    const shift = normalizeBookmarkShift(root.bookmarkCollection.shift_seconds);
    const output = root.querySelector("[data-bookmark-shift-value]");
    const earlier = root.querySelector('[data-bookmark-shift-action="earlier"]');
    const later = root.querySelector('[data-bookmark-shift-action="later"]');
    const reset = root.querySelector('[data-bookmark-shift-action="reset"]');
    if (output) output.textContent = formatBookmarkShift(shift);
    if (earlier) earlier.disabled = shift <= SHIFT_MIN;
    if (later) later.disabled = shift >= SHIFT_MAX;
    if (reset) reset.disabled = shift === 0;
  };

  const updateHighlight = root => {
    if (root.dataset.bookmarkScope !== "media" || !root.bookmarkCollection) return;
    const player = document.getElementById("player");
    if (!player) return;
    const bookmarks = root.bookmarkCollection.bookmarks || [];
    const current = currentBookmarkIndex(bookmarks, player.currentTime);
    root.querySelectorAll(".bookmark-row").forEach((row, index) => {
      row.classList.toggle("current", index === current);
    });
  };

  const renderBookmarks = (root, collection) => {
    root.bookmarkCollection = collection;
    root.bookmarkDirty = false;
    const list = root.querySelector("[data-bookmark-list]");
    const count = root.querySelector("[data-bookmark-count]");
    const bookmarks = [...(collection.bookmarks || [])].sort(
      (a, b) => Number(a.effective_offset_seconds) - Number(b.effective_offset_seconds)
        || Number(a.id) - Number(b.id)
    );
    collection.bookmarks = bookmarks;
    if (count) count.textContent = String(bookmarks.length);
    if (!list) return;
    list.replaceChildren();
    if (!bookmarks.length) {
      const empty = document.createElement("p");
      empty.className = "muted bookmark-empty";
      empty.textContent = "저장된 북마크가 없습니다.";
      list.append(empty);
      updateShiftControls(root);
      return;
    }

    bookmarks.forEach(bookmark => {
      const row = document.createElement("article");
      row.className = "bookmark-row";
      row.dataset.bookmarkId = String(bookmark.id);

      const heading = document.createElement("div");
      heading.className = "bookmark-row-heading";
      if (root.dataset.bookmarkScope === "media") {
        const seek = button(formatBookmarkTime(bookmark.effective_offset_seconds), "bookmark-seek");
        seek.dataset.bookmarkSeek = String(bookmark.id);
        seek.setAttribute("aria-label", `${formatBookmarkTime(bookmark.effective_offset_seconds)} 위치부터 재생`);
        heading.append(seek);
      } else {
        const time = document.createElement("strong");
        time.className = "bookmark-live-time";
        time.textContent = formatBookmarkTime(bookmark.effective_offset_seconds);
        heading.append(time);
      }
      if (!bookmark.resolved && root.dataset.bookmarkScope === "recording") {
        const pending = document.createElement("span");
        pending.className = "status-badge";
        pending.textContent = "녹화 중 시각";
        heading.append(pending);
      }
      row.append(heading);

      const form = document.createElement("form");
      form.className = "bookmark-edit-form";
      form.dataset.bookmarkEdit = String(bookmark.id);

      const timeInput = document.createElement("input");
      timeInput.name = "bookmark_time";
      timeInput.className = "bookmark-time-input";
      timeInput.value = formatBookmarkTime(bookmark.effective_offset_seconds);
      timeInput.inputMode = "numeric";
      timeInput.setAttribute("aria-label", "북마크 시간");
      timeInput.required = true;

      const contentInput = document.createElement("input");
      contentInput.name = "bookmark_content";
      contentInput.className = "bookmark-content-input";
      contentInput.value = bookmark.content || "";
      contentInput.maxLength = 500;
      contentInput.placeholder = "메모 (선택)";
      contentInput.setAttribute("aria-label", "북마크 내용");

      const current = button(
        root.dataset.bookmarkScope === "media" ? "현재 재생 위치" : "현재 방송 시점",
        "bookmark-current"
      );
      current.dataset.bookmarkCurrent = String(bookmark.id);
      const save = document.createElement("button");
      save.type = "submit";
      save.textContent = "저장";
      const remove = button("삭제", "danger bookmark-delete");
      remove.dataset.bookmarkDelete = String(bookmark.id);

      form.append(timeInput, contentInput, current, save, remove);
      row.append(form);
      list.append(row);
    });
    updateShiftControls(root);
    updateHighlight(root);
  };

  const endpointFor = root => root.dataset.bookmarkScope === "media"
    ? `/media/${root.dataset.mediaId}/bookmarks`
    : `/recordings/${root.dataset.sessionId}/bookmarks`;

  const refreshBookmarks = async (root, { silent = false } = {}) => {
    if (root.bookmarkRefreshing || root.bookmarkMutating || root.bookmarkDirty) return;
    root.bookmarkRefreshing = true;
    const revision = root.bookmarkRevision || 0;
    try {
      const collection = await apiRequest(endpointFor(root));
      if (revision !== (root.bookmarkRevision || 0)) return;
      renderBookmarks(root, collection);
      if (!silent) setStatus(root, "");
    } catch (error) {
      setStatus(root, error.message, true);
    } finally {
      root.bookmarkRefreshing = false;
    }
  };

  const runMutation = async (root, url, method, payload, successMessage) => {
    if (root.bookmarkMutating) return false;
    root.bookmarkMutating = true;
    root.bookmarkRevision = (root.bookmarkRevision || 0) + 1;
    try {
      const collection = await apiRequest(url, {
        method,
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
      renderBookmarks(root, collection);
      setStatus(root, successMessage, false, 2500);
      return true;
    } catch (error) {
      setStatus(root, error.message, true, 5000);
      return false;
    } finally {
      root.bookmarkMutating = false;
    }
  };

  const bookmarkById = (root, id) => (root.bookmarkCollection?.bookmarks || [])
    .find(item => Number(item.id) === Number(id));

  const initializeRoot = root => {
    if (initialized.has(root)) return;
    initialized.add(root);
    root.addEventListener("input", event => {
      if (event.target.closest("[data-bookmark-edit]")) root.bookmarkDirty = true;
    });
    root.addEventListener("submit", async event => {
      const addForm = event.target.closest("[data-bookmark-add]");
      const editForm = event.target.closest("[data-bookmark-edit]");
      if (!addForm && !editForm) return;
      event.preventDefault();
      if (addForm) {
        const contentInput = addForm.querySelector('[name="bookmark_content"]');
        const payload = { content: contentInput?.value || "" };
        if (root.dataset.bookmarkScope === "media") {
          const player = document.getElementById("player");
          if (!player || player.readyState < 1) {
            setStatus(root, "영상 프레임이 준비되지 않았습니다.", true, 4000);
            return;
          }
          payload.display_offset_seconds = player.currentTime;
        }
        const saved = await runMutation(
          root,
          endpointFor(root),
          "POST",
          payload,
          "북마크를 저장했습니다."
        );
        if (saved && contentInput) contentInput.value = "";
        return;
      }

      const id = editForm.dataset.bookmarkEdit;
      const timeInput = editForm.querySelector('[name="bookmark_time"]');
      const contentInput = editForm.querySelector('[name="bookmark_content"]');
      const parsedTime = parseBookmarkTime(timeInput?.value);
      if (parsedTime == null) {
        setStatus(root, "시간은 MM:SS 또는 H:MM:SS 형식으로 입력해 주세요.", true, 4000);
        timeInput?.focus();
        return;
      }
      await runMutation(root, `/bookmarks/${id}`, "PATCH", {
        display_offset_seconds: parsedTime,
        content: contentInput?.value || "",
      }, "북마크를 수정했습니다.");
    });

    root.addEventListener("click", async event => {
      const seek = event.target.closest("[data-bookmark-seek]");
      const current = event.target.closest("[data-bookmark-current]");
      const remove = event.target.closest("[data-bookmark-delete]");
      const shiftButton = event.target.closest("[data-bookmark-shift-action]");

      if (seek) {
        const bookmark = bookmarkById(root, seek.dataset.bookmarkSeek);
        const player = document.getElementById("player");
        if (!bookmark || !player) return;
        player.currentTime = Number(bookmark.effective_offset_seconds) || 0;
        try {
          await player.play();
        } catch (_) {
          setStatus(root, "해당 위치로 이동했습니다. 재생 버튼을 눌러 주세요.", false, 3000);
        }
        return;
      }

      if (current) {
        const form = current.closest("[data-bookmark-edit]");
        const content = form?.querySelector('[name="bookmark_content"]')?.value || "";
        const payload = { content };
        if (root.dataset.bookmarkScope === "media") {
          const player = document.getElementById("player");
          if (!player || player.readyState < 1) {
            setStatus(root, "영상 프레임이 준비되지 않았습니다.", true, 4000);
            return;
          }
          payload.display_offset_seconds = player.currentTime;
        } else {
          payload.use_current_live_time = true;
        }
        await runMutation(
          root,
          `/bookmarks/${current.dataset.bookmarkCurrent}`,
          "PATCH",
          payload,
          root.dataset.bookmarkScope === "media"
            ? "현재 재생 위치로 변경했습니다."
            : "현재 방송 시점으로 변경했습니다."
        );
        return;
      }

      if (remove) {
        if (!window.confirm("이 북마크를 삭제할까요? 삭제 후 복구할 수 없습니다.")) return;
        await runMutation(
          root,
          `/bookmarks/${remove.dataset.bookmarkDelete}`,
          "DELETE",
          undefined,
          "북마크를 삭제했습니다."
        );
        return;
      }

      if (shiftButton && root.bookmarkCollection) {
        const currentShift = normalizeBookmarkShift(root.bookmarkCollection.shift_seconds);
        const action = shiftButton.dataset.bookmarkShiftAction;
        const shift = action === "reset"
          ? 0
          : currentShift + (action === "earlier" ? -SHIFT_STEP : SHIFT_STEP);
        await runMutation(
          root,
          `/media/${root.dataset.mediaId}/bookmark-shift`,
          "PUT",
          { shift_seconds: shift },
          "북마크 전체 시간을 보정했습니다."
        );
      }
    });

    refreshBookmarks(root, { silent: true });
  };

  const initializeAll = container => {
    if (container.matches?.("[data-bookmark-root]")) initializeRoot(container);
    container.querySelectorAll?.("[data-bookmark-root]").forEach(initializeRoot);
  };

  initializeAll(document);
  document.body.addEventListener("htmx:afterSwap", event => initializeAll(event.target));

  const player = document.getElementById("player");
  const mediaRoot = document.querySelector('[data-bookmark-scope="media"]');
  if (player && mediaRoot) {
    player.addEventListener("timeupdate", () => updateHighlight(mediaRoot));
    player.addEventListener("seeked", () => updateHighlight(mediaRoot));
    document.addEventListener("keydown", async event => {
      if (!shouldHandleBookmarkShortcut(event) || player.readyState < 1) return;
      event.preventDefault();
      await runMutation(
        mediaRoot,
        endpointFor(mediaRoot),
        "POST",
        { display_offset_seconds: player.currentTime, content: "" },
        "현재 위치에 북마크를 저장했습니다."
      );
    });
  }

  window.setInterval(() => {
    document.querySelectorAll('[data-bookmark-scope="recording"]').forEach(root => {
      if (!root.contains(document.activeElement)) refreshBookmarks(root, { silent: true });
    });
  }, 3000);
})();
