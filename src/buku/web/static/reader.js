/* buku — web reader (Phase 11).
   Drives the chapter iframe, TOC sidebar, prev/next controls, keyboard
   navigation, and scroll-based progress. All progression writes go through
   the central REST progression API (/api/v1/progress/{book}), the same
   ProgressionService store that OPDS sync uses later. 409 conflicts are
   ignored (the stored position is newer — a second device won.)
   Saves happen on navigation, periodically while focused + scrolling, and on
   visibilitychange/pagehide so exiting the page persists the position. */

"use strict";

(() => {
  const root = document.getElementById("reader");
  if (!root) return;

  const bookId = root.dataset.bookId;
  const progressApi = root.dataset.progressApi;
  const chapterCount = parseInt(root.dataset.chapterCount || "0", 10);
  const chapterHrefs = JSON.parse(root.dataset.chapterHrefs || "[]");

  const frame = document.getElementById("reader-frame");
  const frameWrap = document.getElementById("reader-frame-wrap");
  const sidebar = document.getElementById("reader-sidebar");
  const btnToc = document.getElementById("btn-toc");
  const btnTocClose = document.getElementById("btn-toc-close");
  const btnPrev = document.getElementById("btn-prev");
  const btnNext = document.getElementById("btn-next");
  const positionLabel = document.getElementById("reader-position");
  const progressLabel = document.getElementById("reader-progress-label");
  const progressBar = document.getElementById("reader-progress-bar");

  if (!frame) return;

  const titles = collectTitles();
  let chapterIndex = chapterFromFrame();
  let lastSaveAt = 0;
  let lastLocation = "";

  function collectTitles() {
    const map = [];
    document.querySelectorAll(".toc-link").forEach((link) => {
      if (link.dataset.index !== "") {
        map[parseInt(link.dataset.index, 10)] = link.textContent.trim();
      }
    });
    return map;
  }

  function chapterFromFrame() {
    const match = frame.src.match(/\/chapter\/(\d+)/);
    return match ? parseInt(match[1], 10) : 0;
  }

  /* ------------------------------------------------------------- layout */
  function setSidebar(open) {
    if (!sidebar || !btnToc) return;
    sidebar.hidden = !open;
    btnToc.setAttribute("aria-expanded", open ? "true" : "false");
    if (open && frame) frame.focus();
  }

  if (btnToc) btnToc.addEventListener("click", () => setSidebar(true));
  if (btnTocClose) btnTocClose.addEventListener("click", () => setSidebar(false));
  if (sidebar) sidebar.addEventListener("click", (event) => {
    if (event.target === sidebar) setSidebar(false);
  });

  /* ---------------------------------------------------------- navigation */
  function loadChapter(index, fragment) {
    if (index < 0 || index >= chapterCount) return;
    chapterIndex = index;
    frame.src = `/reader/${bookId}/chapter/${index}`;
    updateControls();
    updateLabels();
    if (fragment) {
      frame.addEventListener("load", () => {
        try {
          frame.contentWindow.location.hash = fragment;
        } catch {
          /* same-origin guard; ignore */
        }
      }, { once: true });
    }
    saveProgress("navigation");
  }

  if (btnNext) {
    btnNext.addEventListener("click", () => {
      if (chapterIndex < chapterCount - 1) loadChapter(chapterIndex + 1);
    });
  }
  if (btnPrev) {
    btnPrev.addEventListener("click", () => {
      if (chapterIndex > 0) loadChapter(chapterIndex - 1);
    });
  }

  document.querySelectorAll(".toc-link").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.dataset.index === "") return;
      loadChapter(
        parseInt(link.dataset.index, 10),
        link.dataset.fragment || undefined,
      );
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target && event.target.closest("input, textarea, select")) return;
    if (event.key === "ArrowRight") btnNext && btnNext.click();
    else if (event.key === "ArrowLeft") btnPrev && btnPrev.click();
  });

  function updateControls() {
    if (btnPrev) btnPrev.disabled = chapterIndex <= 0;
    if (btnNext) btnNext.disabled = chapterIndex >= chapterCount - 1;
  }

  function updateLabels() {
    if (!positionLabel) return;
    positionLabel.textContent = titles[chapterIndex]
      || chapterHrefs[chapterIndex]
      || `Chapter ${chapterIndex + 1}`;
  }

  /* ------------------------------------------------------------ progress */
  function chapterFraction() {
    const win = frame.contentWindow;
    const doc = frame.contentDocument;
    if (!win || !doc) return 0;
    const max = doc.documentElement.scrollHeight - win.innerHeight;
    if (max <= 0) return 0;
    return Math.min(1, Math.max(0, win.scrollY / max));
  }

  function currentFragment() {
    const doc = frame.contentDocument;
    if (!doc) return null;
    const x = Math.round(doc.documentElement.clientWidth / 2);
    const y = Math.round((doc.defaultView.scrollY || 0) + Math.min(48, doc.defaultView.innerHeight / 2));
    let el = null;
    try { el = doc.elementFromPoint(x, y); } catch { return null; }
    while (el && !el.id) el = el.parentElement;
    return el && el.id ? el.id : null;
  }

  function locationKey() {
    return `${chapterIndex}:${Math.round(chapterFraction() * 100)}`;
  }

  function lastProgress() {
    const frac = chapterFraction();
    const progression = Math.min(1, (chapterIndex + frac) / chapterCount);
    return {
      progression,
      href: chapterHrefs[chapterIndex] || "",
      title: titles[chapterIndex] || "",
    };
  }

  function renderProgress() {
    const p = lastProgress();
    const pct = Math.round(p.progression * 100);
    if (progressLabel) progressLabel.textContent = `${pct}%`;
    if (progressBar) progressBar.style.width = `${pct}%`;
  }

  async function saveProgress(reason) {
    if (!chapterCount || !progressApi) return;
    const key = locationKey();
    if (key === lastLocation && reason !== "navigation") return;
    lastLocation = key;
    lastSaveAt = Date.now();
    const payload = {
      ...lastProgress(),
      fragment: currentFragment() || null,
      modified_at: new Date().toISOString(),
      device_id: "buku-web",
      device_name: "buku browser reader",
    };
    try {
      const response = await fetch(progressApi, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (response.status === 409) {
        /* stored position is newer (other device); keep reading locally */
      }
    } catch {
      /* transient network failure — the periodic timer will retry */
    }
  }

  let lastSeenKey = locationKey();
  function pollProgress() {
    const key = locationKey();
    if (key === lastSeenKey) return;
    lastSeenKey = key;
    renderProgress();
    const now = Date.now();
    if (now - lastSaveAt >= 5000) saveProgress("periodic");
  }
  let timer = null;
  if (frameWrap) {
    timer = setInterval(pollProgress, 1500);
    frameWrap.addEventListener("scroll", () => { lastSeenKey = locationKey(); }, { passive: true });
  }
  frame.addEventListener("load", () => {
    applyResumeFragment();
    renderProgress();
  });

  function applyResumeFragment() {
    const fragment = frame.dataset.resumeFragment;
    if (!fragment) return;
    frame.dataset.resumeFragment = "";
    try {
      frame.contentWindow.location.hash = fragment;
      saveProgress("resume");
    } catch {
      /* ignore */
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") saveProgress("hidden");
  });
  window.addEventListener("pagehide", () => saveProgress("pagehide"));
  window.addEventListener("beforeunload", () => saveProgress("unload"));

  /* --------------------------------------------------------------- boot */
  updateControls();
  updateLabels();
  renderProgress();
})();