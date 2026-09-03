(() => {
  "use strict";

  const data = window.TRAVEL_VIDEO_DATA;
  if (!data?.videos?.length) {
    document.querySelector("#workspace").innerHTML = '<p class="empty">시안 데이터가 없습니다.</p>';
    return;
  }

  const workspace = document.querySelector("#workspace");
  const toast = document.querySelector("[data-toast]");
  const shortcutDialog = document.querySelector("[data-shortcuts]");
  const preferredVideo = data.videos[1] || data.videos[0];
  const preferredScene = preferredVideo.scenes.find((scene) => scene.highlight) || preferredVideo.scenes[0];
  const state = {
    concept: "desk",
    videoId: preferredVideo.id,
    sceneId: preferredScene.id,
    inspectorTab: "analysis",
    query: "",
    highlightsOnly: false,
  };

  document.querySelector("[data-project-title]").textContent = data.projectTitle;

  const esc = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const currentVideo = () => data.videos.find((video) => video.id === state.videoId) || data.videos[0];
  const currentScene = () => {
    const video = currentVideo();
    return video.scenes.find((scene) => scene.id === state.sceneId) || video.scenes[0];
  };

  const formatBytes = (bytes) => {
    if (!Number.isFinite(bytes)) return "—";
    if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(2)} GB`;
    return `${Math.round(bytes / 1_000_000)} MB`;
  };

  const confidence = (value) => {
    const score = Number(value || 0);
    return `<span class="confidence ${score >= .9 ? "high" : ""}">${Math.round(score * 100)}%</span>`;
  };

  const searchIcon = () => `
    <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="7" cy="7" r="4.5" stroke="currentColor"/>
      <path d="m10.5 10.5 3 3" stroke="currentColor" stroke-linecap="round"/>
    </svg>`;

  const playIcon = () => `
    <svg viewBox="0 0 12 12" fill="none" aria-hidden="true">
      <path d="M3 2.2v7.6L9.2 6 3 2.2Z" fill="currentColor"/>
    </svg>`;

  const searchField = (placeholder) => `
    <label class="search-field">
      <span hidden>검색</span>${searchIcon()}
      <input type="search" value="${esc(state.query)}" placeholder="${esc(placeholder)}" data-search autocomplete="off">
    </label>`;

  const sceneSearchText = (scene) => [
    scene.label,
    scene.headline,
    scene.action,
    scene.dialogue,
    ...scene.languages,
    ...scene.notables.flatMap((item) => [item.title, item.description, item.edit_hint]),
    ...scene.segments.flatMap((segment) => [segment.visual, ...segment.actions, ...Object.values(segment.transcripts).flat()]),
  ].join(" ").toLocaleLowerCase("ko");

  const videoSearchText = (video) => [video.source, video.title, video.oneLine, video.narrative, ...video.tags].join(" ").toLocaleLowerCase("ko");
  const matches = (text) => !state.query || text.includes(state.query.toLocaleLowerCase("ko"));
  const visibleScenes = (video) => video.scenes.filter((scene) => {
    const queryMatch = matches(sceneSearchText(scene));
    const highlightMatch = !state.highlightsOnly || scene.highlight;
    return queryMatch && highlightMatch;
  });

  const clipList = (className = "asset-list") => {
    const rows = data.videos
      .filter((video) => matches(videoSearchText(video)) || video.scenes.some((scene) => matches(sceneSearchText(scene))))
      .map((video, index) => `
        <li class="asset-row">
          <button type="button" data-video="${esc(video.id)}" aria-current="${video.id === state.videoId}">
            <img src="${esc(video.representativeFrame)}" alt="" loading="lazy">
            <span class="asset-copy">
              <strong>${esc(video.title)}</strong>
              <span>${esc(video.source)}</span>
              <span class="asset-meta"><b>${String(index + 1).padStart(2, "0")}</b><b>${esc(video.durationLabel)}</b></span>
            </span>
          </button>
        </li>`).join("");
    return `<ul class="${className}">${rows || '<li class="empty">검색 결과 없음</li>'}</ul>`;
  };

  const viewer = (video, scene) => {
    const progress = Math.max(0, Math.min(100, scene.start / video.duration * 100));
    return `
      <div class="media-placeholder">
        <img src="${esc(scene.representativeFrame)}" alt="${esc(scene.label)} 대표 프레임">
        <div class="viewer-overlay">
          <span class="timecode">${esc(scene.representativeTime)}</span>
          <span class="proxy-state">프록시 없음 · 스틸 프리뷰</span>
        </div>
      </div>
      <div class="transport">
        <button class="play-button" type="button" data-play aria-label="선택 구간 재생">${playIcon()}</button>
        <div class="scrub" style="--progress:${progress}%" aria-label="영상 내 선택 구간 위치"></div>
        <span class="timecode">${esc(scene.startTime)}–${esc(scene.endTime)}</span>
      </div>`;
  };

  const storyStrip = (scene) => {
    if (!scene.keyframes.length) return '<p class="empty">선택된 대표 프레임 없음</p>';
    return `<div class="story-strip">${scene.keyframes.map((frame) => `
      <figure class="story-frame">
        <img src="${esc(frame.frame)}" alt="${esc(frame.role)}" loading="lazy">
        <figcaption>${esc(frame.timecode)} · ${esc(frame.role)}</figcaption>
      </figure>`).join("")}</div>`;
  };

  const rawTranscript = (scene) => {
    const rows = scene.segments.map((segment) => {
      const lines = Object.entries(segment.transcripts)
        .flatMap(([language, items]) => items.map((text) => `
          <div class="transcript-line"><span class="lang">${esc(language)}</span><p>${esc(text)}</p></div>`))
        .join("");
      return `
        <li>
          <span class="timecode">${esc(segment.time)}</span>
          <div>
            <p>${esc(segment.visual)}</p>
            ${lines ? `<div class="transcript-pair">${lines}</div>` : '<span class="chip">음성 없음</span>'}
          </div>
        </li>`;
    }).join("");
    return rows ? `<ol class="segment-log">${rows}</ol>` : '<p class="empty">원시 세그먼트 없음</p>';
  };

  const notableBlocks = (scene) => scene.notables.length
    ? scene.notables.map((item) => `
        <div class="notable-box">
          <strong>${esc(item.timecode)} · ${esc(item.title)}</strong>
          <p>${esc(item.description)}</p>
          <p class="edit-hint">편집: ${esc(item.edit_hint)}</p>
        </div>`).join("")
    : '<p>별도 특이 포인트 없음. 연결 장면이나 호흡 조절 구간으로 검토.</p>';

  const inspectorBody = (scene) => {
    if (state.inspectorTab === "dialogue") {
      return `
        <div class="field-group">
          <div class="field-label"><span>종합 대화 해석</span><span>${scene.languages.map(esc).join(" + ") || "현장음"}</span></div>
          <p>${esc(scene.dialogue)}</p>
        </div>
        <div class="field-group">
          <div class="field-label"><span>원시 STT 후보 · 구간별</span><span>미검증 포함</span></div>
          ${rawTranscript(scene)}
        </div>`;
    }
    if (state.inspectorTab === "frames") {
      return `
        <div class="field-group">
          <div class="field-label"><span>맥락 프레임</span><span>${scene.keyframes.length}장</span></div>
          ${storyStrip(scene)}
        </div>
        <div class="field-group">
          <div class="field-label"><span>대표 프레임 선정 이유</span><span>${esc(scene.representativeTime)}</span></div>
          <p>${esc(scene.representativeReason || "선정 이유 없음")}</p>
        </div>`;
    }
    return `
      <div class="field-group">
        <div class="field-label"><span>무슨 일이 일어나는가</span><span>${esc(scene.id)} · ${confidence(scene.confidence)}</span></div>
        <h3>${esc(scene.headline)}</h3>
        <p>${esc(scene.action)}</p>
      </div>
      <div class="field-group">
        <div class="field-label"><span>특이 포인트 / 편집 가치</span><span>${scene.notables.length}</span></div>
        ${notableBlocks(scene)}
      </div>
      <div class="field-group">
        <div class="field-label"><span>스토리보드</span><span>${scene.keyframes.length}장</span></div>
        ${storyStrip(scene)}
      </div>`;
  };

  const inspector = (video, scene, className = "inspector-panel") => `
    <aside class="panel ${className}" aria-label="선택 장면 검사기">
      <div class="panel-titlebar"><h2>Source Inspector</h2><span class="count">${esc(scene.id)}</span></div>
      ${viewer(video, scene)}
      <div class="tabs" role="tablist" aria-label="장면 상세 보기">
        <button type="button" role="tab" data-tab="analysis" aria-selected="${state.inspectorTab === "analysis"}">분석</button>
        <button type="button" role="tab" data-tab="dialogue" aria-selected="${state.inspectorTab === "dialogue"}">대화</button>
        <button type="button" role="tab" data-tab="frames" aria-selected="${state.inspectorTab === "frames"}">프레임</button>
      </div>
      <div class="scroll inspector-content">${inspectorBody(scene)}</div>
    </aside>`;

  const renderDesk = () => {
    const video = currentVideo();
    const scene = currentScene();
    const scenes = visibleScenes(video);
    const ruler = video.scenes.map((item) => `
      <button class="ruler-section ${item.id === scene.id ? "is-active" : ""}" type="button" data-scene="${esc(item.id)}" style="flex:${Math.max(8, item.duration)}">
        <strong>${esc(item.id)}</strong><span>${esc(item.startTime)}</span>
      </button>`).join("");
    const sceneRows = scenes.map((item) => `
      <li class="scene-row">
        <button type="button" data-scene="${esc(item.id)}" aria-current="${item.id === scene.id}">
          <span class="scene-time"><strong>${esc(item.startTime)}</strong><span>+${Math.round(item.duration)}s</span></span>
          <span class="scene-thumb"><img src="${esc(item.representativeFrame)}" alt="" loading="lazy"><span class="marker">${esc(item.representativeTime)}</span></span>
          <span class="scene-primary"><strong>${esc(item.headline)}</strong><p>${esc(item.action)}</p></span>
          <span class="scene-dialogue"><span class="field-label">대화 ${item.languages.map(esc).join("/") || "—"}</span><p>${esc(item.dialogue)}</p></span>
          <span class="scene-signals">${item.highlight ? '<span class="chip highlight">HIGHLIGHT</span>' : '<span class="chip">SCENE</span>'}${confidence(item.confidence)}</span>
        </button>
      </li>`).join("");
    workspace.className = "workspace edit-desk";
    workspace.innerHTML = `
      <aside class="panel bin-panel">
        <div class="panel-titlebar"><h2>Footage</h2><span class="count">${data.videos.length} clips</span></div>
        <div class="bin-search">${searchField("클립·장면·대화 검색  /")}</div>
        <div class="scroll">${clipList()}</div>
      </aside>
      <section class="panel timeline-panel" aria-label="장면 타임라인">
        <header class="clip-header">
          <div><h1>${esc(video.title)}</h1><p>${esc(video.oneLine)}</p></div>
          <div class="clip-head-meta"><span>${esc(video.captureTime)}</span><span>${esc(video.durationLabel)}</span><span>${esc(video.resolution)}</span><span>${esc(formatBytes(video.sizeBytes))}</span></div>
        </header>
        <div class="scene-ruler" aria-label="클립 구간 탐색">${ruler}</div>
        <div class="scroll"><ol class="scene-list">${sceneRows || '<li class="empty">일치하는 장면 없음</li>'}</ol></div>
      </section>
      ${inspector(video, scene)}
      <footer class="statusbar"><span class="state-ok">분석 데이터 연결됨 · 원본 미변경</span><span>${video.sceneCount} scenes · ${video.notableCount} notable · ${esc(video.codec)}</span></footer>`;
  };

  const renderLedger = () => {
    const video = currentVideo();
    const scene = currentScene();
    const scenes = visibleScenes(video);
    const strip = data.videos.map((item, index) => `
      <button class="strip-item" type="button" data-video="${esc(item.id)}" aria-current="${item.id === video.id}">
        <img src="${esc(item.representativeFrame)}" alt="" loading="lazy">
        <span class="strip-copy"><span>${String(index + 1).padStart(2, "0")} · ${esc(item.captureTime)}</span><strong>${esc(item.title)}</strong><span>${esc(item.durationLabel)} · ${item.sceneCount} scenes</span></span>
      </button>`).join("");
    const rows = scenes.map((item) => `
      <tr class="${item.id === scene.id ? "is-active" : ""}" data-scene-row="${esc(item.id)}">
        <td><button class="ledger-select" type="button" data-scene="${esc(item.id)}"><strong class="timecode">${esc(item.startTime)}</strong><span>${esc(item.endTime)}</span></button></td>
        <td><img class="table-frame" src="${esc(item.representativeFrame)}" alt="" loading="lazy"></td>
        <td><strong class="cell-title">${esc(item.headline)}</strong><p class="cell-copy">${esc(item.action)}</p></td>
        <td><strong class="cell-title">${item.languages.map(esc).join(" + ") || "현장음"}</strong><p class="cell-copy">${esc(item.dialogue)}</p></td>
        <td>${item.notables.length ? `<strong class="cell-title cell-note">${esc(item.notables[0].title)}</strong><p class="cell-copy">${esc(item.notables[0].edit_hint)}</p>` : '<span class="chip">연결 구간</span>'}</td>
        <td>${confidence(item.confidence)}${item.highlight ? '<span class="chip highlight">H</span>' : ""}</td>
      </tr>`).join("");
    workspace.className = "workspace review-ledger";
    workspace.innerHTML = `
      <nav class="asset-strip" aria-label="영상 목록">${strip}</nav>
      <div class="ledger-toolbar">
        ${searchField("전체 로그 검색  /")}
        <span class="view-summary"><strong>${esc(video.title)}</strong> · ${esc(video.oneLine)}</span>
        <div class="ledger-filter"><button class="small-button" type="button" data-highlights aria-pressed="${state.highlightsOnly}">하이라이트만</button><span class="count">${scenes.length}/${video.sceneCount}</span></div>
      </div>
      <div class="ledger-body">
        <div class="table-wrap">
          <table class="scene-table">
            <thead><tr><th class="col-time">SOURCE</th><th class="col-frame">FRAME</th><th class="col-action">ACTION / EVENT</th><th class="col-dialogue">DIALOGUE</th><th class="col-edit">EDIT VALUE</th><th class="col-score">SCORE</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="6" class="empty">일치하는 장면 없음</td></tr>'}</tbody>
          </table>
        </div>
        ${inspector(video, scene, "ledger-inspector")}
      </div>
      <footer class="ledger-footer"><span>Review Ledger · 비교와 선별 중심</span><span>${data.videos.length} clips · ${data.videos.reduce((sum, item) => sum + item.sceneCount, 0)} scenes</span></footer>`;
  };

  const consoleTranscriptLines = (scene) => {
    const candidates = [];
    scene.segments.forEach((segment) => {
      Object.entries(segment.transcripts).forEach(([language, items]) => {
        items.forEach((text) => candidates.push({ language, text, time: segment.time }));
      });
    });
    const lines = candidates.slice(0, 5).map((item) => `
      <div class="script-line"><span class="lang">${esc(item.language)}</span><p><span class="timecode">${esc(item.time)}</span> ${esc(item.text)}</p></div>`).join("");
    return lines || `<div class="script-line"><span class="lang">SYN</span><p>${esc(scene.dialogue)}</p></div>`;
  };

  const renderConsole = () => {
    const video = currentVideo();
    const scene = currentScene();
    const scenes = visibleScenes(video);
    const takes = data.videos.map((item, index) => `
      <li><button type="button" data-video="${esc(item.id)}" aria-current="${item.id === video.id}"><span class="take-number">${String(index + 1).padStart(2, "0")}</span><span class="take-copy"><strong>${esc(item.title)}</strong><span>${esc(item.captureTime)} · ${esc(item.durationLabel)}</span></span></button></li>`).join("");
    const scripts = scenes.map((item) => `
      <li class="script-scene ${item.id === scene.id ? "is-active" : ""}">
        <div class="script-gutter"><strong>${esc(item.startTime)}</strong><span>${Math.round(item.duration)} SEC</span></div>
        <div class="script-body">
          <div class="script-title"><button type="button" data-scene="${esc(item.id)}">${esc(item.headline)}</button><span>${item.highlight ? '<span class="chip highlight">SELECT</span>' : confidence(item.confidence)}</span></div>
          <p class="script-action">${esc(item.action)}</p>
          <div class="script-lines">${consoleTranscriptLines(item)}</div>
          ${item.notables.length ? `<div class="script-note"><span>EDIT</span><span>${esc(item.notables[0].edit_hint)}</span></div>` : ""}
        </div>
      </li>`).join("");
    const evidence = scene.segments.map((segment) => `
      <li><span>${esc(segment.time)}</span><p>${esc(segment.visual)}${segment.actions.length ? ` · ${segment.actions.map(esc).join(", ")}` : ""}</p></li>`).join("");
    workspace.className = "workspace cut-console";
    workspace.innerHTML = `
      <aside class="panel console-bin">
        <div class="console-project"><strong>TAKES · 시간순</strong><span>${data.videos.length} CLIPS / ${data.videos.reduce((sum, item) => sum + item.sceneCount, 0)} SCENES</span></div>
        <div class="scroll"><ol class="take-list">${takes}</ol></div>
      </aside>
      <section class="panel transcript-panel" aria-label="대화와 사건 컷 시트">
        <header class="console-head"><div><h1>${esc(video.title)}</h1><p>${esc(video.narrative)}</p></div><span class="head-time">${esc(video.captureTime)} / ${esc(video.durationLabel)}</span></header>
        <div class="console-tools">${searchField("대사·행동·편집 포인트 검색  /")}<div class="legend"><span><i></i>대화 근거</span><span><i></i>편집 포인트</span></div></div>
        <div class="scroll"><ol class="script-sheet">${scripts || '<li class="empty">일치하는 장면 없음</li>'}</ol></div>
      </section>
      <aside class="panel console-monitor">
        <div class="console-monitor-head"><strong>VISUAL EVIDENCE</strong><span class="range">${esc(scene.startTime)}–${esc(scene.endTime)}</span></div>
        ${viewer(video, scene)}
        <div class="monitor-details">
          <h2>${esc(scene.label)}</h2><p>${esc(scene.eventDescription)}</p>
          <div class="field-group"><div class="field-label"><span>맥락 프레임</span><span>${scene.keyframes.length}</span></div>${storyStrip(scene)}</div>
          <div class="field-group"><div class="field-label"><span>세그먼트 행동 로그</span><span>${scene.segments.length}</span></div><ul class="evidence-list">${evidence}</ul></div>
          <div class="field-group"><div class="field-label"><span>특이 포인트</span><span>${scene.notables.length}</span></div>${notableBlocks(scene)}</div>
        </div>
      </aside>
      <footer class="console-footer"><span>Cut Console · 대화와 사건 중심</span><span>RAW STT 후보 표시 · 요약과 구분</span></footer>`;
  };

  const restoreFocus = (selector, value) => {
    requestAnimationFrame(() => {
      const element = workspace.querySelector(`${selector}[data-scene="${CSS.escape(value)}"]`);
      element?.focus({ preventScroll: true });
    });
  };

  const render = () => {
    document.body.dataset.concept = state.concept;
    document.querySelectorAll("[data-concept-button]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.conceptButton === state.concept));
    });
    if (state.concept === "ledger") renderLedger();
    else if (state.concept === "console") renderConsole();
    else renderDesk();
  };

  const showToast = (message) => {
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("is-visible"), 2600);
  };

  document.addEventListener("click", (event) => {
    const conceptButton = event.target.closest("[data-concept-button]");
    if (conceptButton) {
      state.concept = conceptButton.dataset.conceptButton;
      render();
      return;
    }
    const videoButton = event.target.closest("[data-video]");
    if (videoButton) {
      const video = data.videos.find((item) => item.id === videoButton.dataset.video);
      if (!video) return;
      state.videoId = video.id;
      state.sceneId = (video.scenes.find((item) => item.highlight) || video.scenes[0]).id;
      render();
      return;
    }
    const sceneButton = event.target.closest("[data-scene]");
    if (sceneButton) {
      state.sceneId = sceneButton.dataset.scene;
      render();
      restoreFocus("", state.sceneId);
      return;
    }
    const row = event.target.closest("[data-scene-row]");
    if (row) {
      state.sceneId = row.dataset.sceneRow;
      render();
      return;
    }
    const tab = event.target.closest("[data-tab]");
    if (tab) {
      state.inspectorTab = tab.dataset.tab;
      render();
      requestAnimationFrame(() => workspace.querySelector(`[data-tab="${state.inspectorTab}"]`)?.focus());
      return;
    }
    if (event.target.closest("[data-play]")) {
      const scene = currentScene();
      showToast(`프록시 연결 전입니다. 재생 요청 범위 ${scene.startTime}–${scene.endTime}`);
      return;
    }
    const highlightButton = event.target.closest("[data-highlights]");
    if (highlightButton) {
      state.highlightsOnly = !state.highlightsOnly;
      render();
      return;
    }
    if (event.target.closest("[data-help]")) shortcutDialog.showModal();
  });

  document.addEventListener("input", (event) => {
    if (!event.target.matches("[data-search]")) return;
    state.query = event.target.value.trim();
    render();
    requestAnimationFrame(() => {
      const input = workspace.querySelector("[data-search]");
      input?.focus();
      input?.setSelectionRange(input.value.length, input.value.length);
    });
  });

  document.addEventListener("keydown", (event) => {
    const typing = event.target.matches("input, textarea, [contenteditable]");
    if (!typing && ["1", "2", "3"].includes(event.key)) {
      state.concept = ({ "1": "desk", "2": "ledger", "3": "console" })[event.key];
      render();
      return;
    }
    if (!typing && event.key === "/") {
      event.preventDefault();
      workspace.querySelector("[data-search]")?.focus();
      return;
    }
    if (typing && event.key === "Escape") {
      state.query = "";
      render();
      return;
    }
    if (!typing && ["ArrowUp", "ArrowDown"].includes(event.key)) {
      event.preventDefault();
      const video = currentVideo();
      const index = video.scenes.findIndex((scene) => scene.id === state.sceneId);
      const delta = event.key === "ArrowDown" ? 1 : -1;
      const nextScene = video.scenes[Math.max(0, Math.min(video.scenes.length - 1, index + delta))];
      if (nextScene) {
        state.sceneId = nextScene.id;
        render();
      }
    }
  });

  render();
})();
