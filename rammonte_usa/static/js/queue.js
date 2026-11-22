/* Client-side lists: watch queue, watch later, history (YouTube-style). */
(function () {
  const MAX = 100;
  const LISTS = { queue: "rammonte-queue", watchlater: "rammonte-watchlater", history: "rammonte-history" };

  function read(name) {
    try { return JSON.parse(localStorage.getItem(LISTS[name]) || "[]"); }
    catch (e) { return []; }
  }
  function write(name, list) {
    try { localStorage.setItem(LISTS[name], JSON.stringify(list.slice(0, MAX))); } catch (e) {}
    updateBadges();
    document.dispatchEvent(new CustomEvent("vlist:change", { detail: { name, list } }));
  }

  function makeList(name, opts) {
    opts = opts || {};
    return {
      name: name,
      list: function () { return read(name); },
      add: function (item) {
        if (!item || !item.id) return false;
        const list = read(name);
        if (opts.noDupes && list.some(x => String(x.id) === String(item.id))) return false;
        // For history, move to front if exists
        if (name === "history") {
          const filtered = list.filter(x => String(x.id) !== String(item.id));
          filtered.unshift(normalize(item));
          write(name, filtered);
          return true;
        }
        list.push(normalize(item));
        write(name, list);
        return true;
      },
      remove: function (id) { write(name, read(name).filter(x => String(x.id) !== String(id))); },
      clear: function () { write(name, []); },
      has: function (id) { return read(name).some(x => String(x.id) === String(id)); },
      nextAfter: function (id) {
        const list = read(name);
        const i = list.findIndex(x => String(x.id) === String(id));
        if (i < 0 || i + 1 >= list.length) return null;
        return list[i + 1];
      }
    };
  }

  function normalize(item) {
    return { id: item.id, title: item.title || "", url: item.url || "", thumb: item.thumb || "", duration: item.duration || "" };
  }

  window.VLists = { read, write, makeList };
  window.Queue = makeList("queue", { noDupes: true });
  window.WatchLater = makeList("watchlater", { noDupes: true });
  window.History = makeList("history");

  // backwards compat alias
  window.Queue.list = window.Queue.list || function () { return read("queue"); };

  function updateBadges() {
    const q = read("queue").length;
    document.querySelectorAll("[data-queue-badge]").forEach(el => { el.textContent = q; el.style.display = q ? "" : "none"; });
    const w = read("watchlater").length;
    document.querySelectorAll("[data-watchlater-badge]").forEach(el => { el.textContent = w; el.style.display = w ? "" : "none"; });
  }

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  function cardData(card) {
    const wrap = card.closest(".video-card-wrap") || card.parentElement;
    const titleEl = wrap.querySelector(".video-title");
    const thumbImg = wrap.querySelector(".video-thumbnail img");
    const durEl = wrap.querySelector(".video-duration");
    const href = card.getAttribute("href") || "";
    const m = href.match(/\/(\d+)(?:\/|$)/);
    const id = m ? m[1] : null;
    return { id, title: titleEl ? titleEl.textContent.trim() : "", url: href, thumb: thumbImg ? thumbImg.getAttribute("src") : "", duration: durEl ? durEl.textContent.trim() : "" };
  }

  function showToast(msg) {
    let t = document.getElementById("queue-toast");
    if (!t) { t = document.createElement("div"); t.id = "queue-toast"; t.className = "queue-toast"; document.body.appendChild(t); }
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove("show"), 1600);
  }
  window.showToast = showToast;

  function injectCardButtons() {
    document.querySelectorAll(".video-card").forEach(card => {
      const wrap = card.closest(".video-card-wrap");
      if (!wrap || wrap.querySelector(".queue-add-btn")) return;
      const data = cardData(card);
      if (!data.id) return;
      // queue button
      const qbtn = document.createElement("button");
      qbtn.type = "button";
      qbtn.className = "queue-add-btn";
      qbtn.title = "Add to queue";
      qbtn.innerHTML = "☰+";
      qbtn.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        const added = Queue.add(data);
        qbtn.classList.toggle("queued", Queue.has(data.id));
        showToast(added ? "Added to queue" : "Already in queue");
      });
      if (Queue.has(data.id)) qbtn.classList.add("queued");
      // watch later button
      const wbtn = document.createElement("button");
      wbtn.type = "button";
      wbtn.className = "wl-add-btn";
      wbtn.title = "Save to Watch later";
      wbtn.innerHTML = "⏱";
      wbtn.style.cssText = "position:absolute;top:8px;right:40px;z-index:3;background:rgba(0,0,0,.6);color:#fff;border:none;border-radius:6px;padding:2px 6px;font-size:12px;cursor:pointer;";
      wbtn.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        const added = WatchLater.add(data);
        wbtn.classList.toggle("saved", WatchLater.has(data.id));
        showToast(added ? "Saved to Watch later" : "Already saved");
      });
      if (WatchLater.has(data.id)) wbtn.classList.add("saved");
      const dl = wrap.querySelector(".card-download");
      if (dl && dl.parentNode) { dl.parentNode.insertBefore(qbtn, dl); dl.parentNode.insertBefore(wbtn, dl); }
      else { wrap.appendChild(qbtn); wrap.appendChild(wbtn); }
    });
  }

  window.QueueUI = {
    render: function (container, listName) {
      listName = listName || "queue";
      if (!container) return;
      const L = listName === "queue" ? Queue : (listName === "watchlater" ? WatchLater : History);
      const list = L.list();
      container.innerHTML = "";
      const title = listName === "queue" ? "Queue is empty" : (listName === "watchlater" ? "Watch later is empty" : "History is empty");
      const hint = listName === "queue" ? "Add videos with the “☰+” button on any video card." :
                   (listName === "watchlater" ? "Save videos with the “⏱” button on any video card." : "Videos you watch appear here.");
      if (!list.length) {
        container.innerHTML = '<div class="empty-state"><span class="empty-icon">☰</span><h2>' + title + '</h2><p>' + hint + '</p></div>';
        return;
      }
      const play = document.createElement("a");
      play.className = "btn btn-primary";
      play.href = list[0].url;
      play.textContent = listName === "history" ? "Play first" : "Play all";
      play.style.marginRight = ".5rem";
      container.appendChild(play);

      const ul = document.createElement("div");
      ul.className = "queue-list";
      list.forEach((item, idx) => {
        const row = document.createElement("div");
        row.className = "queue-item";
        row.innerHTML =
          '<span class="queue-index">' + (idx + 1) + '</span>' +
          (item.thumb ? '<img class="queue-thumb" src="' + item.thumb + '" alt="">' : '<span class="queue-thumb placeholder">▶</span>') +
          '<div class="queue-info"><a class="queue-title" href="' + item.url + '">' + escapeHtml(item.title) + '</a>' +
          (item.duration ? '<span class="queue-dur">' + item.duration + '</span>' : '') + '</div>' +
          '<button type="button" class="queue-remove" data-id="' + item.id + '" aria-label="Remove">✕</button>';
        ul.appendChild(row);
      });
      container.appendChild(ul);
      ul.querySelectorAll(".queue-remove").forEach(b => {
        b.addEventListener("click", () => { L.remove(b.dataset.id); window.QueueUI.render(container, listName); });
      });
      if (listName !== "history") {
        const clear = document.createElement("button");
        clear.type = "button"; clear.className = "btn btn-outline"; clear.textContent = "Clear";
        clear.style.marginTop = "1rem";
        clear.addEventListener("click", () => { L.clear(); window.QueueUI.render(container, listName); });
        container.appendChild(clear);
      } else {
        const clear = document.createElement("button");
        clear.type = "button"; clear.className = "btn btn-outline"; clear.textContent = "Clear history";
        clear.style.marginTop = "1rem";
        clear.addEventListener("click", () => { L.clear(); window.QueueUI.render(container, listName); });
        container.appendChild(clear);
      }
    }
  };

  window.QueueAutoAdvance = function (currentId) {
    const next = Queue.nextAfter(currentId);
    if (!next || !next.url) return;
    Queue.remove(currentId);
    window.location.href = next.url;
  };

  // Record a view in history when on a detail page
  window.recordHistory = function (item) {
    if (!item || !item.id) return;
    History.add(item);
  };

  document.addEventListener("DOMContentLoaded", () => {
    injectCardButtons();
    updateBadges();
  });
  // re-render on changes
  document.addEventListener("vlist:change", () => updateBadges());
})();
