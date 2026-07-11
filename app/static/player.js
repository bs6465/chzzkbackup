(() => {
  const player = document.getElementById("player");
  const list = document.getElementById("chat-list");
  const search = document.getElementById("chat-search");
  const autoScroll = document.getElementById("auto-scroll");
  const filters = document.getElementById("chat-types");
  let rows = [], enabled = new Set(), visible = [], active = -1;
  const fmt = value => { const n = Number(value || 0); return `${String(Math.floor(n / 60)).padStart(2,"0")}:${String(Math.floor(n % 60)).padStart(2,"0")}`; };
  const render = () => {
    const term = search.value.trim().toLowerCase();
    visible = rows.filter(row => enabled.has(row.type) && (!term || `${row.nickname} ${row.content}`.toLowerCase().includes(term)));
    list.innerHTML = visible.length ? visible.map((row, index) => `<button class="chat-row ${row.sync_state}" data-index="${index}" ${row.offset_seconds == null ? "disabled" : ""}><time>${row.offset_seconds == null ? "누락" : fmt(row.offset_seconds)}</time><span><strong>${escapeHtml(row.nickname)}</strong>${escapeHtml(row.content)}</span></button>`).join("") : '<p class="muted">표시할 채팅이 없습니다.</p>';
  };
  const escapeHtml = text => { const node=document.createElement("span"); node.textContent=text; return node.innerHTML; };
  fetch(`/media/${window.CHZZK_MEDIA_ID}/chat`).then(r => r.json()).then(data => {
    rows = data; [...new Set(rows.map(row => row.type))].forEach(type => { enabled.add(type); filters.insertAdjacentHTML("beforeend", `<label><input type="checkbox" value="${escapeHtml(type)}" checked> ${escapeHtml(type)}</label>`); }); render();
  }).catch(() => { list.innerHTML='<p class="error">채팅을 불러오지 못했습니다.</p>'; });
  filters.addEventListener("change", event => { event.target.checked ? enabled.add(event.target.value) : enabled.delete(event.target.value); render(); });
  search.addEventListener("input", render);
  list.addEventListener("click", event => { const button=event.target.closest("button[data-index]"); if(!button)return; const row=visible[Number(button.dataset.index)]; if(row.offset_seconds!=null){player.currentTime=row.offset_seconds; player.play();} });
  player.addEventListener("timeupdate", () => { let found=-1; for(let i=0;i<visible.length;i++){if(visible[i].offset_seconds!=null&&visible[i].offset_seconds<=player.currentTime)found=i;else if(visible[i].offset_seconds!=null)break;} if(found===active)return; list.querySelector(".current")?.classList.remove("current"); active=found; const element=list.querySelector(`[data-index='${found}']`); if(element){element.classList.add("current"); if(autoScroll.checked)element.scrollIntoView({block:"center"});} });
})();
