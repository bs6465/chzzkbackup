(() => {
  const normalizedSeconds = value => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : 0;
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

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { formatTimelineTime, screenshotFilename };
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
  let rows = [], enabled = new Set(), visible = [], active = -1;
  let captureStatusTimer;
  const render = () => {
    const term = search.value.trim().toLowerCase();
    visible = rows.filter(row => enabled.has(row.type) && (!term || `${row.nickname} ${row.content}`.toLowerCase().includes(term)));
    list.innerHTML = visible.length ? visible.map((row, index) => `<button class="chat-row ${row.sync_state}" data-index="${index}" ${row.offset_seconds == null ? "disabled" : ""}><time>${row.offset_seconds == null ? "누락" : formatTimelineTime(row.offset_seconds)}</time><span><strong>${escapeHtml(row.nickname)}</strong>${escapeHtml(row.content)}</span></button>`).join("") : '<p class="muted">표시할 채팅이 없습니다.</p>';
  };
  const escapeHtml = text => { const node=document.createElement("span"); node.textContent=text; return node.innerHTML; };
  fetch(`/media/${window.CHZZK_MEDIA_ID}/chat`).then(r => r.json()).then(data => {
    rows = data; [...new Set(rows.map(row => row.type))].forEach(type => { enabled.add(type); filters.insertAdjacentHTML("beforeend", `<label><input type="checkbox" value="${escapeHtml(type)}" checked> ${escapeHtml(type)}</label>`); }); render();
  }).catch(() => { list.innerHTML='<p class="error">채팅을 불러오지 못했습니다.</p>'; });
  filters.addEventListener("change", event => { event.target.checked ? enabled.add(event.target.value) : enabled.delete(event.target.value); render(); });
  search.addEventListener("input", render);
  list.addEventListener("click", event => { const button=event.target.closest("button[data-index]"); if(!button)return; const row=visible[Number(button.dataset.index)]; if(row.offset_seconds!=null){player.currentTime=row.offset_seconds; player.play();} });
  player.addEventListener("timeupdate", () => { let found=-1; for(let i=0;i<visible.length;i++){if(visible[i].offset_seconds!=null&&visible[i].offset_seconds<=player.currentTime)found=i;else if(visible[i].offset_seconds!=null)break;} if(found===active)return; list.querySelector(".current")?.classList.remove("current"); active=found; const element=list.querySelector(`[data-index='${found}']`); if(element){element.classList.add("current"); if(autoScroll.checked)element.scrollIntoView({block:"center"});} });
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
