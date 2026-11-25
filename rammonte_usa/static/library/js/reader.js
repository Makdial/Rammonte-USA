/* Library PDF reader — continuous, scrollable pages.
 * - Crisp rendering (devicePixelRatio-aware, capped for iOS limits)
 * - Lazy render around the viewport, pages pruned when far away
 * - Placeholder skeletons so nothing looks "broken" while loading
 * - Falls back to opening the PDF in a new tab if a page cannot render
 * - Text selection triggers context-aware translation
 */
(function(){
  "use strict";

  var pdfUrl = window.LIBRARY_PDF_URL || "";
  var startPage = Math.max(1, window.LIBRARY_START_PAGE || 1);
  var slug = window.LIBRARY_SLUG || "";
  var translateUrl = window.LIBRARY_TRANSLATE_URL;
  var progressUrl = window.LIBRARY_PROGRESS_URL;
  var bookmarkUrl = window.LIBRARY_BOOKMARK_URL;
  var csrf = window.LIBRARY_CSRF;
  var sourceLang = window.LIBRARY_SOURCE_LANG || "auto";

  var readerMain = document.getElementById("readerMain");
  var pagesContainer = document.getElementById("pdfPages");
  var nativeFrame = document.getElementById("nativeFrame");
  var statusEl = document.getElementById("readerStatus");
  var pageInfo = document.getElementById("pageInfo");
  var pageInfoMobile = document.getElementById("pageInfoMobile");
  var zoomLevel = document.getElementById("zoomLevel");

  var pdfDoc = null;
  var numPages = 0;
  var scale = 1.2;
  var pageNum = startPage;
  var pageDims = {};
  var baseDims = null;
  var pagePromises = {};
  var renderedAt = {};
  var inflight = {};
  var visibleRatios = {};
  var useNative = false;
  var nativeSwitched = false;
  var fullDocText = "";
  var zoomTimer = null;
  var saveTimer = null;
  var dimsCursor = 2;

  var RENDER_RADIUS = 3;
  var PRUNE_RADIUS = 8;
  var DPR_CAP = 2;
  var MAX_CANVAS_PX = 4096;

  function setStatus(msg){ if (statusEl) statusEl.textContent = msg; }
  function fail(msg){ console.error("[reader]", msg); setStatus(msg); }

  /* ---------------- Translation ---------------- */
  var translationBox = document.getElementById("translationBox");
  var trOriginal = document.getElementById("translationOriginal");
  var trResult = document.getElementById("translationResult");
  var translateToggle = document.getElementById("translateToggle");
  var targetLang = document.getElementById("targetLang");
  var viewMode = document.getElementById("viewMode");
  var translationToast = document.getElementById("translationToast");
  var lastTranslatedText = "";
  var lastTranslatedPage = pageNum;
  var lastTranslationFailed = false;
  var selectionTimer = null;
  var toastTimer = null;
  var translateToken = 0;

  function applyViewMode(){
    if (!translationBox) return;
    var mode = viewMode ? viewMode.value : "sidebyside";
    translationBox.classList.remove("mode-original","mode-translated","mode-sidebyside");
    translationBox.classList.add("mode-" + mode);
    trOriginal.hidden = (mode === "translated");
    trResult.hidden = (mode === "original");
  }
  if (viewMode) { viewMode.addEventListener("change", applyViewMode); applyViewMode(); }

  var translateEnabled = translateToggle ? translateToggle.checked : false;
  if (translateToggle) {
    translateToggle.addEventListener("change", function(){
      translateEnabled = translateToggle.checked;
      if (!translateEnabled) clearSelectionHighlight();
    });
  }

  /* Keep the translated sentence visibly marked in the PDF. The CSS Custom
     Highlight API survives the native selection disappearing (clicking the
     target-language dropdown, tapping elsewhere, etc.); browsers without it
     fall back to a class on the text-layer spans. */
  function clearSelectionHighlight(){
    if (window.CSS && CSS.highlights) {
      try { CSS.highlights.delete("library-translation"); } catch (e) { /* ignore */ }
    }
    pagesContainer.querySelectorAll(".text-layer span.translation-selected").forEach(function(span){
      span.classList.remove("translation-selected");
    });
  }

  function markSelection(sel){
    clearSelectionHighlight();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return;
    var range = sel.getRangeAt(0);
    try {
      if (window.CSS && CSS.highlights && typeof window.Highlight === "function") {
        CSS.highlights.set("library-translation", new Highlight(range.cloneRange()));
        return;
      }
    } catch (e) { /* fall through to the class-based highlight */ }
    var anchor = sel.anchorNode;
    var el = anchor && (anchor.nodeType === 1 ? anchor : anchor.parentElement);
    var pageEl = el && el.closest ? el.closest(".pdf-page") : null;
    if (!pageEl) return;
    pageEl.querySelectorAll(".text-layer span").forEach(function(span){
      try {
        if (range.intersectsNode(span)) span.classList.add("translation-selected");
      } catch (e) { /* ignore */ }
    });
  }

  function pageForNode(node){
    var el = node && (node.nodeType === 1 ? node : node.parentElement);
    while (el && el !== pagesContainer) {
      if (el.dataset && el.dataset.page) return parseInt(el.dataset.page, 10) || pageNum;
      el = el.parentElement;
    }
    return pageNum;
  }

  function selectionInsideReader(sel){
    if (!sel || !sel.rangeCount) return false;
    return !!(sel.anchorNode && sel.focusNode &&
      pagesContainer.contains(sel.anchorNode) && pagesContainer.contains(sel.focusNode));
  }

  function translationTarget(){
    var target = targetLang ? targetLang.value : "";
    if (!target || target === "auto") target = "en";
    return target;
  }

  function showToast(original, translated){
    if (!translationToast || !window.matchMedia("(max-width: 900px)").matches) return;
    var o = translationToast.querySelector(".tt-original");
    var t = translationToast.querySelector(".tt-result");
    if (o) o.textContent = original;
    if (t) t.textContent = translated;
    translationToast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, 12000);
  }

  function hideToast(){
    if (!translationToast) return;
    translationToast.hidden = true;
    clearTimeout(toastTimer);
  }

  if (translationToast) {
    var toastClose = document.getElementById("closeTranslationToast");
    if (toastClose) toastClose.addEventListener("click", hideToast);
  }

  function doTranslate(text, pageForContext, sel, force){
    if (!text || text.length < 2 || !translationBox) return;
    if (!force && text === lastTranslatedText && !lastTranslationFailed) return;
    lastTranslatedText = text;
    lastTranslatedPage = pageForContext || pageNum;
    lastTranslationFailed = false;
    if (sel) markSelection(sel);
    trOriginal.textContent = text;
    trResult.textContent = "...";
    translationBox.hidden = false;
    // If the sidebar was scrolled down to the controls, bring the newly
    // populated panel back into view.
    if (window.matchMedia("(min-width: 901px)").matches && translationBox.scrollIntoView) {
      translationBox.scrollIntoView({block: "nearest"});
    }
    hideToast();
    // On phones the panel lives in the off-canvas sidebar, so show the wait
    // state in the toast immediately.
    if (window.matchMedia("(max-width: 900px)").matches) showToast(text, "Translating...");
    var token = ++translateToken;
    var ctx = useNative ? fullDocText : pageText(lastTranslatedPage);
    fetch(translateUrl, {
      method: "POST",
      headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
      body: JSON.stringify({
        text: text,
        source: sourceLang,
        target: translationTarget(),
        context: ctx,
        book: slug,
        page: lastTranslatedPage
      })
    }).then(function(r){return r.json();}).then(function(data){
      if (token !== translateToken) return;
      if (data.translated) {
        trResult.textContent = data.translated;
        showToast(text, data.translated);
      } else {
        lastTranslationFailed = true;
        trResult.textContent = data.error || "Translation unavailable";
        showToast(text, data.error || "Translation unavailable");
      }
    }).catch(function(){
      if (token !== translateToken) return;
      lastTranslationFailed = true;
      trResult.textContent = "Translation unavailable";
      showToast(text, "Translation unavailable");
    });
  }

  function maybeTranslate(win, sel){
    if (!translateEnabled) return;
    sel = sel || (win.getSelection ? win.getSelection() : null);
    if (!sel || sel.isCollapsed) return;
    var text = sel.toString().trim();
    if (!text) return;
    doTranslate(text, pageForNode(sel.anchorNode), sel);
  }

  pagesContainer.addEventListener("mouseup", function(){ maybeTranslate(window); });
  pagesContainer.addEventListener("touchend", function(){
    clearTimeout(selectionTimer);
    selectionTimer = setTimeout(function(){ maybeTranslate(window); }, 300);
  }, {passive: true});

  // Mobile browsers often show selection handles without firing mouseup.
  // Wait until the selection stops changing, then translate it.
  document.addEventListener("selectionchange", function(){
    if (!translateEnabled) return;
    var sel = window.getSelection ? window.getSelection() : null;
    if (!sel || sel.isCollapsed || !selectionInsideReader(sel)) return;
    clearTimeout(selectionTimer);
    selectionTimer = setTimeout(function(){
      var current = window.getSelection ? window.getSelection() : null;
      if (current && !current.isCollapsed && selectionInsideReader(current)) {
        maybeTranslate(window, current);
      }
    }, 750);
  });

  if (targetLang) {
    targetLang.addEventListener("change", function(){
      // Re-translate the current selection immediately when the target
      // language changes, even if the browser has collapsed the selection.
      var sel = window.getSelection ? window.getSelection() : null;
      if (sel && !sel.isCollapsed && selectionInsideReader(sel)) {
        doTranslate(sel.toString().trim(), pageForNode(sel.anchorNode), sel, true);
      } else if (lastTranslatedText) {
        doTranslate(lastTranslatedText, lastTranslatedPage, null, true);
      }
    });
  }

  var closeTranslation = document.getElementById("closeTranslation");
  if (closeTranslation) {
    closeTranslation.addEventListener("click", function(){
      if (translationBox) translationBox.hidden = true;
      clearSelectionHighlight();
      hideToast();
      lastTranslatedText = "";
      lastTranslationFailed = false;
    });
  }

  if (nativeFrame) {
    nativeFrame.addEventListener("load", function(){
      try {
        var win = nativeFrame.contentWindow;
        if (win && win.document) win.document.addEventListener("mouseup", function(){ maybeTranslate(win); });
      } catch (e) { /* not same-origin */ }
    });
  }

  /* ---------------- Shells ---------------- */
  function shell(num){
    return pagesContainer.querySelector('.pdf-page[data-page="' + num + '"]');
  }
  function pageText(num){
    var el = shell(num);
    var tl = el && el.querySelector(".text-layer");
    return tl ? (tl.textContent || "") : "";
  }
  function getPage(num){
    if (!pagePromises[num]) pagePromises[num] = pdfDoc.getPage(num);
    return pagePromises[num];
  }
  function pixelFactor(vp){
    var f = Math.min(window.devicePixelRatio || 1, DPR_CAP);
    var longest = Math.max(vp.width, vp.height);
    if (longest * f > MAX_CANVAS_PX) f = MAX_CANVAS_PX / longest;
    return Math.max(f, 0.5);
  }
  function sizeShell(num, d){
    var el = shell(num);
    if (!el || !d) return;
    el.style.width = Math.round(d.width) + "px";
    el.style.height = Math.round(d.height) + "px";
  }
  function showSkeleton(el){
    if (el.querySelector(".pdf-skeleton")) return;
    var s = document.createElement("div");
    s.className = "pdf-skeleton";
    el.appendChild(s);
  }
  function hideSkeleton(el){
    if (!el) return;
    var s = el.querySelector(".pdf-skeleton");
    if (s) s.remove();
  }

  /* ---------------- Native viewer fallback ---------------- */
  function enableNativeMode(){
    if (useNative) return;
    useNative = true;
    nativeSwitched = true;
    pagesContainer.hidden = true;
    nativeFrame.hidden = false;
    if (pdfUrl) nativeFrame.src = pdfUrl;
    setStatus("Some pages could not be rendered in-app. Using the browser's PDF viewer.");
    if (pdfDoc && !fullDocText) {
      var parts = [];
      var chain = Promise.resolve();
      for (var n = 1; n <= numPages; n++) {
        (function(pageNum){
          chain = chain.then(function(){
            return pdfDoc.getPage(pageNum).then(function(p){ return p.getTextContent(); });
          }).then(function(tc){
            parts[pageNum - 1] = (tc.items || []).map(function(i){return i.str;}).join(" ");
          }).catch(function(){ parts[pageNum - 1] = ""; });
        })(n);
      }
      chain.then(function(){ fullDocText = parts.join("\n\n"); });
    }
  }

  function failPage(num, err){
    var el = shell(num);
    if (!el) return;
    hideSkeleton(el);
    if (el.querySelector(".page-failed")) return;
    var box = document.createElement("div");
    box.className = "page-failed";
    var p = document.createElement("p");
    p.textContent = "Page " + num + " could not be rendered.";
    if (err && err.message) {
      var small = document.createElement("small");
      small.textContent = err.message;
      small.style.display = "block";
      small.style.opacity = "0.7";
      small.style.fontSize = ".75rem";
      small.style.marginTop = ".25rem";
      box.appendChild(small);
    }
    var a = document.createElement("a");
    a.href = pdfUrl || "#";
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Open the PDF directly";
    box.appendChild(p);
    box.appendChild(a);
    el.appendChild(box);
    console.error("[reader] page " + num + " failed:", err);
    if (!nativeSwitched) enableNativeMode();
  }

  function buildShell(num){
    var el = shell(num);
    if (el) return el;
    el = document.createElement("div");
    el.className = "pdf-page";
    el.dataset.page = num;
    el.appendChild(document.createElement("canvas"));
    var tl = document.createElement("div");
    tl.className = "text-layer";
    el.appendChild(tl);
    if (baseDims) { sizeShell(num, {width: baseDims.width * scale, height: baseDims.height * scale}); }
    pagesContainer.appendChild(el);
    showSkeleton(el);
    if (observer) observer.observe(el);
    return el;
  }

  /* ---------------- Rendering ---------------- */
  var renderTokens = {};
  var renderingAt = {};

  /* Create a brand-new canvas for every render. This guarantees PDF.js can
     never see the same canvas reused while another render() is in progress
     (the cause of "Cannot use the same canvas during multiple render()
     operations"), and it releases the old backing store right away. */
  function freshCanvas(el){
    var old = el.querySelector("canvas");
    var c = document.createElement("canvas");
    if (old && old.parentNode) old.parentNode.replaceChild(c, old);
    else el.insertBefore(c, el.firstChild);
    return c;
  }

  function renderTextLayer(num, token){
    getPage(num).then(function(page){
      if (token !== renderTokens[num] || useNative) return;
      var el = shell(num);
      if (!el) return;
      var tl = el.querySelector(".text-layer");
      if (!tl) return;
      updateScaleVar();
      var vpCss = page.getViewport({scale: scale});
      var textPromise = page.getTextContent();
      // The old spans are about to be discarded, so any highlight range that
      // pointed into them would be stale.
      if (lastTranslatedPage === num) clearSelectionHighlight();
      tl.innerHTML = "";
      textPromise.then(function(textContent){
        if (token !== renderTokens[num] || useNative) return;
        try {
          if (typeof pdfjsLib.renderTextLayer === "function") {
            pdfjsLib.renderTextLayer({textContent: textContent, container: tl, viewport: vpCss, textDivs: []});
          } else {
            textContent.items.forEach(function(item){
              var tx = pdfjsLib.Util.transform(vpCss.transform, item.transform);
              var span = document.createElement("span");
              span.textContent = item.str;
              span.style.left = tx[4] + "px";
              span.style.top = (tx[5] - item.height) + "px";
              span.style.fontSize = item.height + "px";
              tl.appendChild(span);
            });
          }
        } catch (e) { console.warn("[reader] text layer", e); }
      }).catch(function(err){ console.warn("[reader] text content", err); });
    }).catch(function(err){ console.warn("[reader] getPage text", err); });
  }

  function renderPage(num){
    if (!pdfDoc || useNative) return;
    var el = buildShell(num);
    var want = Math.round(scale * 100);
    // Already rendered, or already being painted at this exact scale: don't
    // cancel and restart it (that caused endless re-rendering while scrolling).
    if (renderedAt[num] === want || renderingAt[num] === want) return;

    // Invalidate any in-progress render for this page and stop it early.
    if (inflight[num]) {
      try { inflight[num].cancel(); } catch (e) { /* ignore */ }
      delete inflight[num];
    }
    renderTokens[num] = (renderTokens[num] || 0) + 1;
    var token = renderTokens[num];
    renderingAt[num] = want;

    function isCurrent(){ return token === renderTokens[num] && !useNative; }

    function attempt(withAnnotations){
      if (!isCurrent()) return Promise.resolve();
      return getPage(num).then(function(page){
        if (!isCurrent()) return;
        var vpCss = page.getViewport({scale: scale});
        var f = pixelFactor(vpCss);
        pageDims[num] = {width: vpCss.width, height: vpCss.height};
        sizeShell(num, pageDims[num]);

        var vpPix = page.getViewport({scale: scale * f});
        var canvas = freshCanvas(el);
        canvas.width = Math.round(vpPix.width);
        canvas.height = Math.round(vpPix.height);
        canvas.style.width = Math.round(vpCss.width) + "px";
        canvas.style.height = Math.round(vpCss.height) + "px";

        var opts = {canvasContext: canvas.getContext("2d"), viewport: vpPix};
        if (!withAnnotations) opts.annotationMode = 0; // AnnotationMode.DISABLE
        var task = page.render(opts);
        inflight[num] = task;
        return task.promise.then(function(){
          if (!isCurrent()) return;
          renderedAt[num] = want;
          delete renderingAt[num];
          hideSkeleton(el);
          setStatus("");
        }).catch(function(err){
          if (err && err.name === "RenderingCancelledException") return;
          if (!isCurrent()) return;
          if (withAnnotations) {
            // Some PDFs contain malformed annotations. Retry once with them
            // disabled, on a fresh canvas.
            console.warn("[reader] p" + num + " retry without annotations:", err);
            return attempt(false);
          }
          throw err;
        }).finally(function(){
          if (inflight[num] === task) delete inflight[num];
        });
      });
    }

    showSkeleton(el);
    attempt(true).catch(function(err){
      if (!isCurrent()) return;
      console.error("[reader] render p" + num + " failed:", err);
      failPage(num, err);
    }).finally(function(){
      if (renderingAt[num] === want && renderedAt[num] !== want) delete renderingAt[num];
    });

    renderTextLayer(num, token);
  }

  function clearPage(num){
    var el = shell(num);
    if (!el) return;
    if (inflight[num]) {
      try { inflight[num].cancel(); } catch (e) { /* ignore */ }
      delete inflight[num];
    }
    renderTokens[num] = (renderTokens[num] || 0) + 1; // invalidate pending work
    delete renderingAt[num];
    freshCanvas(el);
    delete renderedAt[num];
    showSkeleton(el);
  }

  function renderVisible(){
    var lo = Math.max(1, pageNum - RENDER_RADIUS);
    var hi = Math.min(numPages, pageNum + RENDER_RADIUS);
    for (var n = lo; n <= hi; n++) renderPage(n);
    var plo = Math.max(1, pageNum - PRUNE_RADIUS);
    var phi = Math.min(numPages, pageNum + PRUNE_RADIUS);
    Object.keys(renderedAt).forEach(function(k){
      var n = parseInt(k, 10);
      if (n < plo || n > phi) clearPage(n);
    });
  }

  var observer = null;
  var resizeTimer = null;

  /* On desktop readerMain is the scroll container; on phones the whole
     document scrolls instead (readerMain grows to fit its content). Using a
     non-scrolling element as IntersectionObserver root makes every page count
     as visible, which renders the entire book at once and exhausts memory.
     Pick the real scroller, or null (= viewport). */
  function scrollRoot(){
    if (!readerMain) return null;
    var style = window.getComputedStyle(readerMain);
    if (style.overflowY === "visible") return null;
    if (readerMain.scrollHeight <= readerMain.clientHeight + 2) return null;
    return readerMain;
  }

  function setupObserver(){
    if (observer) observer.disconnect();
    visibleRatios = {};
    observer = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        var num = parseInt(en.target.dataset.page, 10);
        visibleRatios[num] = en.isIntersecting ? en.intersectionRatio : 0;
        if (en.isIntersecting) renderPage(num);
      });
      var best = 0, bestNum = pageNum;
      Object.keys(visibleRatios).forEach(function(k){
        var r = visibleRatios[k] || 0;
        if (r > best) { best = r; bestNum = parseInt(k, 10); }
      });
      if (best > 0 && bestNum !== pageNum) {
        pageNum = bestNum;
        updatePageInfo();
        scheduleSave();
      }
      renderVisible();
    }, {root: scrollRoot(), rootMargin: "900px 0px", threshold: [0, 0.1, 0.3, 0.6, 0.9, 1]});
    pagesContainer.querySelectorAll(".pdf-page").forEach(function(el){ observer.observe(el); });
  }

  function preloadDims(){
    if (!pdfDoc || dimsCursor > numPages) return;
    var batch = 0;
    function next(){
      if (dimsCursor > numPages || batch >= 8) { requestIdle(preloadDims); return; }
      var n = dimsCursor++;
      batch++;
      getPage(n).then(function(page){
        if (baseDims) {
          var vp = page.getViewport({scale: 1});
          var d = {width: vp.width, height: vp.height};
          if (Math.abs(d.width - baseDims.width) > 2 || Math.abs(d.height - baseDims.height) > 2) {
            pageDims[n] = {width: d.width * scale, height: d.height * scale};
            if (renderedAt[n] === undefined) sizeShell(n, pageDims[n]);
          }
        }
        next();
      }).catch(function(){ next(); });
    }
    next();
  }
  function requestIdle(fn){
    if (window.requestIdleCallback) window.requestIdleCallback(fn, {timeout: 800});
    else setTimeout(fn, 200);
  }

  /* ---------------- Navigation / progress ---------------- */
  function goToPage(num){
    num = Math.min(Math.max(1, num), numPages || 1);
    var el = shell(num) || buildShell(num);
    el.scrollIntoView({behavior: "smooth", block: "start"});
    pageNum = num;
    updatePageInfo();
    scheduleSave();
    renderVisible();
  }
  function updatePageInfo(){
    var txt = (pdfDoc ? pageNum : 1) + " / " + (numPages || 1);
    if (pageInfo) pageInfo.textContent = txt;
    if (pageInfoMobile) pageInfoMobile.textContent = txt;
  }
  function scheduleSave(){
    if (!progressUrl) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function(){
      var percent = numPages ? (pageNum / numPages * 100) : 0;
      fetch(progressUrl, {
        method: "POST",
        headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
        body: JSON.stringify({page: pageNum, percent: percent})
      }).catch(function(){});
    }, 800);
  }

  /* ---------------- Zoom / fit ---------------- */
  function updateScaleVar(){
    if (pagesContainer) pagesContainer.style.setProperty("--scale-factor", String(scale));
  }
  function scheduleZoomRender(delay){
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(renderVisible, delay);
  }
  function applyScale(next, opts){
    if (useNative || !pdfDoc) return;
    scale = Math.max(0.4, Math.min(3, next));
    updateScaleVar();
    if (zoomLevel) zoomLevel.textContent = Math.round(scale * 100) + "%";
    pagesContainer.querySelectorAll(".pdf-page").forEach(function(el){
      var n = parseInt(el.dataset.page, 10);
      var d = pageDims[n];
      if (!d && baseDims) d = {width: baseDims.width * scale, height: baseDims.height * scale};
      sizeShell(n, d);
      if (renderedAt[n] !== undefined) { delete renderedAt[n]; }
    });
    if (opts && opts.keepPage) {
      requestAnimationFrame(function(){ goToPage(pageNum); });
    }
    scheduleZoomRender(160);
  }
  function fitToWidth(){
    if (useNative || !pdfDoc) return;
    getPage(1).then(function(page){
      var vp = page.getViewport({scale: 1});
      var target = Math.min((readerMain ? readerMain.clientWidth : window.innerWidth) - 28, 1400);
      applyScale(target / vp.width, {keepPage: true});
    });
  }
  function autoFitIfSmall(){
    var w = readerMain ? readerMain.clientWidth : window.innerWidth;
    if (!useNative && pdfDoc && w < 1024) fitToWidth();
  }

  /* ---------------- Controls ---------------- */
  function bind(id, fn){
    var el = document.getElementById(id);
    if (el) el.addEventListener("click", fn);
  }
  bind("prevPage", function(){ goToPage(pageNum - 1); });
  bind("nextPage", function(){ goToPage(pageNum + 1); });
  bind("zoomIn", function(){ applyScale(scale + 0.2, {keepPage: true}); });
  bind("zoomOut", function(){ applyScale(scale - 0.2, {keepPage: true}); });
  bind("fitWidth", fitToWidth);

  var darkToggle = document.getElementById("darkMode");
  var readerEl = document.getElementById("reader");
  if (darkToggle) darkToggle.addEventListener("change", function(){
    readerEl.classList.toggle("dark", darkToggle.checked);
  });

  var sidebar = document.getElementById("readerSidebar");
  var toggleSidebar = document.getElementById("toggleSidebar");
  var backdrop = null;
  function openDrawer(){
    if (!sidebar) return;
    sidebar.classList.add("open");
    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.className = "reader-backdrop";
      backdrop.addEventListener("click", closeDrawer);
      document.body.appendChild(backdrop);
    }
  }
  function closeDrawer(){
    if (sidebar) sidebar.classList.remove("open");
    if (backdrop) { backdrop.remove(); backdrop = null; }
  }
  if (toggleSidebar) toggleSidebar.addEventListener("click", function(){
    if (sidebar && sidebar.classList.contains("open")) closeDrawer(); else openDrawer();
  });

  bind("addBookmark", function(){
    fetch(bookmarkUrl, {
      method: "POST",
      headers: {"Content-Type":"application/json","X-CSRFToken": csrf},
      body: JSON.stringify({page: pageNum, note: ""})
    }).then(function(r){return r.json();}).then(function(d){
      if (d.ok) setStatus("Bookmark added on page " + pageNum);
    }).catch(function(){});
  });

  bind("fullscreenBtn", function(){
    var el = document.getElementById("readerMain");
    if (el && el.requestFullscreen) el.requestFullscreen();
  });

  bind("nativeToggle", function(){
    if (useNative) {
      if (pdfUrl) window.open(pdfUrl, "_blank", "noopener");
    } else {
      enableNativeMode();
    }
  });

  document.addEventListener("keydown", function(e){
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT")) return;
    if (useNative) return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); goToPage(pageNum - 1); }
    else if (e.key === "ArrowRight" || e.key === "PageDown") { e.preventDefault(); goToPage(pageNum + 1); }
    else if (e.key === "+" || e.key === "=") applyScale(scale + 0.2, {keepPage: true});
    else if (e.key === "-") applyScale(scale - 0.2, {keepPage: true});
  });

  window.addEventListener("resize", function(){
    autoFitIfSmall();
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(setupObserver, 250);
  });

  /* ---------------- Load ---------------- */
  if (typeof pdfjsLib === "undefined") {
    fail("PDF.js failed to load.");
    if (pdfUrl) window.open(pdfUrl, "_blank", "noopener");
    return;
  }

  setStatus("Loading document...");
  updateScaleVar();
  if (zoomLevel) zoomLevel.textContent = Math.round(scale * 100) + "%";
  pdfjsLib.getDocument({url: pdfUrl, disableRange: true, disableStream: true}).promise.then(function(doc){
    pdfDoc = doc;
    numPages = doc.numPages;
    for (var n = 1; n <= numPages; n++) buildShell(n);
    updatePageInfo();
    getPage(1).then(function(page){
      var vp = page.getViewport({scale: 1});
      baseDims = {width: vp.width, height: vp.height};
      pagesContainer.querySelectorAll(".pdf-page").forEach(function(el){
        var n = parseInt(el.dataset.page, 10);
        sizeShell(n, {width: baseDims.width * scale, height: baseDims.height * scale});
      });
      updateScaleVar();
      if (window.innerWidth < 1024) fitToWidth();
      // Start observing only after every page has its real aspect ratio.
      // Otherwise the initially tiny shells all count as visible and the
      // whole book gets rendered at once (an out-of-memory risk on phones).
      setupObserver();
      requestAnimationFrame(function(){
        goToPage(startPage);
        renderVisible();
        preloadDims();
      });
    });
  }).catch(function(err){
    console.error("[reader] PDF load error:", err);
    if (pdfUrl) window.open(pdfUrl, "_blank", "noopener");
    fail("Failed to load PDF: " + (err && err.message ? err.message : err) + ". Opened the PDF in a new tab.");
  });
})();
