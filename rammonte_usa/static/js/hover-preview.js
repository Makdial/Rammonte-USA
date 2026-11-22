/* YouTube-style animated preview on hover over a video card. */
(function () {
  const canHover = window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  if (!canHover) return;

  let activeVideo = null;

  function stopPreview() {
    if (activeVideo) {
      try { activeVideo.pause(); } catch (e) {}
      if (activeVideo.parentNode) activeVideo.parentNode.removeChild(activeVideo);
      activeVideo = null;
    }
  }

  function startPreview(card) {
    const url = card.getAttribute("data-preview-url");
    if (!url) return;
    const wrap = card.closest(".video-card-wrap") || card;
    const thumb = wrap.querySelector(".video-thumbnail");
    if (!thumb) return;
    stopPreview();
    const v = document.createElement("video");
    v.muted = true;
    v.playsInline = true;
    v.preload = "metadata";
    v.src = url;
    v.className = "hover-preview-video";
    v.style.position = "absolute";
    v.style.inset = "0";
    v.style.width = "100%";
    v.style.height = "100%";
    v.style.objectFit = "cover";
    v.style.zIndex = "2";
    thumb.style.position = "relative";
    thumb.appendChild(v);
    activeVideo = v;
    // play only first ~6s then stop
    v.addEventListener("loadedmetadata", () => {
      try { v.currentTime = 0; } catch (e) {}
      v.play().catch(() => {});
    });
    v.addEventListener("timeupdate", () => {
      if (v.currentTime >= 6) { v.pause(); v.currentTime = 0; }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".video-card[data-preview-url]").forEach(card => {
      card.addEventListener("mouseenter", () => startPreview(card));
      card.addEventListener("mouseleave", stopPreview);
    });
  });

  // pause when tab hidden
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPreview();
  });
})();
