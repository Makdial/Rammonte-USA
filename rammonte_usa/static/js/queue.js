/* Lightweight client-side "watch queue" (YouTube-style Up Next).
   Stored in localStorage so it survives navigation and reloads. */
(function () {
  const KEY = "rammonte-queue";
  const MAX = 100;

  function read() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); }
    catch (e) { return []; }
  }
  function write(list) {
    try { localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX))); } catch (e) {}
    updateBadge();
    document.dispatchEvent(new CustomEvent("queue:change", { detail: { list } }));
  }

  function idOf(item) { return String(item.id); }

  window.Queue = {
    list: read,
    add: function (item) {
      if (!item || !item.id) return false;
      const list = read();
      if (list.some(x => idOf(x) === idOf(item))) return false; // no dupes
      list.push({
        id: item.id,
        title: item.title || "",
        url: item.url || "",
        thumb: item.thumb || "",
        duration: item.duration || ""
      });
      write(list);
      return true;
    },
    remove: function (id) {
      write(read().filter(x => idOf(x) !== String(id)));
    },
    clear: function () { write([]); },
    has: function (id) { return read().some(x => idOf(x) === String(id)); },
    nextAfter: function (id) {
      const list = read();
      const i = list.findIndex(x => idOf(x) === String(id));
      if (i < 0 || i + 1 >= list.length) return null;
      return list[i + 1];
    }
  };

  function updateBadge() {
    const n = read().length;
    document.querySelectorAll("[data-queue-badge]").forEach(el => {
      el.textContent = n;
      el.style.display = n ? "" : "none";
    });
  }

  // Inject "Add to queue" buttons into video cards.
  function cardData(card) {
    const wrap = card.closest(".video-card-wrap") || card.parentElement;
    const titleEl = wrap.querySelector(".video-title");
    const thumbImg = wrap.querySelector(".video-thumbnail img");
    const durEl = wrap.querySelector(".video-duration");
    const href = card.getAttribute("href") || "";
    const m = href.match(/\/videos\/(\d+)/) || href.match(/\/(\d+)(?:\/|$)/);
    const id = m ? m[1] : null;
    return {
      id: id,
      title: titleEl ? titleEl.textContent.trim() : "",
      url: href,
      thumb: thumbImg ? thumbImg.getAttribute("src") : "",
      duration: durEl ? durEl.textContent.trim() : ""
    };
  }

  function injectCardButtons() {
    document.querySelectorAll(".video-card").forEach(card => {
      const wrap = card.closest(".video-card-wrap");
      if (!wrap || wrap.querySelector(".queue-add-btn")) return;
      const data = cardData(card);
      if (!data.id) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "queue-add-btn";
      btn.setAttribute("aria-label", "Add to queue");
      btn.title = "Add to queue";
      btn.innerHTML = " ☰+ ";
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const added = Queue.add(data);
        btn.classList.toggle("queued", Queue.has(data.id));
        btn.textContent = Queue.has(data.id) ? "✓" : "☰+";
        showToast(added ? "Added to queue" : "Already in queue");
      });
      if (Queue.has(data.id)) { btn.classList.add("queued"); btn.textContent = "✓"; }
      // place near download button if present, else append
      const dl = wrap.querySelector(".card-download");
      if (dl && dl.parentNode) dl.parentNode.insertBefore(btn, dl);
      else wrap.appendChild(btn);
    });
  }

  function showToast(msg) {
    window.showToast = showToast;
    let t = document.getElementById("queue-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "queue-toast";
      t.className = "queue-toast";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove("show"), 1600);
  }
  window.showToast = showToast;

  // Render a queue list into a container element.
  window.QueueUI = {
    render: function (container) {
      if (!container) return;
      const list = read();
      container.innerHTML = "";
      if (!list.length) {
        container.innerHTML = '<div class="empty-state"><span class="empty-icon">☰</span><h2>Queue is empty</h2><p>Add videos with the "Add to queue" button on any video card.</p></div>';
        return;
      }
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
      // remove handlers
      ul.querySelectorAll(".queue-remove").forEach(b => {
        b.addEventListener("click", () => { Queue.remove(b.dataset.id); QueueUI.render(container); });
      });
      // clear button
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "btn btn-outline";
      clear.textContent = "Clear queue";
      clear.style.marginTop = "1rem";
      clear.addEventListener("click", () => { Queue.clear(); QueueUI.render(container); });
      container.appendChild(clear);
      // play all
      if (list.length) {
        const play = document.createElement("a");
        play.className = "btn btn-primary";
        play.href = list[0].url;
        play.textContent = "Play all";
        play.style.marginRight = ".5rem";
        container.insertBefore(play, ul);
      }
    }
  };

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  // Auto-advance on the detail page when the current video ends.
  window.QueueAutoAdvance = function (currentId) {
    const next = Queue.nextAfter(currentId);
    if (!next || !next.url) return;
    // remove current from queue to avoid loops
    Queue.remove(currentId);
    window.location.href = next.url;
  };

  document.addEventListener("DOMContentLoaded", () => {
    injectCardButtons();
    updateBadge();
  });
})();
