/*
 * Offline support helpers.
 *
 * - Registers the service worker so the app shell loads without a connection.
 * - Wires the "Save offline" button on a video page to the browser cache.
 * - Renders the saved-audio list on the /offline/ page.
 */
(function () {
    "use strict";

    var MEDIA_CACHE = "fazakkir-media-v1";
    var STORE_KEY = "fazakkir-offline";

    function readStore() {
        try {
            var raw = localStorage.getItem(STORE_KEY);
            var list = raw ? JSON.parse(raw) : [];
            return Array.isArray(list) ? list : [];
        } catch (e) {
            return [];
        }
    }

    function writeStore(list) {
        try {
            localStorage.setItem(STORE_KEY, JSON.stringify(list));
        } catch (e) {
            /* storage unavailable */
        }
    }

    function upsert(entry) {
        var list = readStore().filter(function (item) {
            return item.url !== entry.url;
        });
        list.unshift(entry);
        writeStore(list);
    }

    function remove(url) {
        writeStore(
            readStore().filter(function (item) {
                return item.url !== url;
            })
        );
    }

    function mediaCache() {
        if (!("caches" in window)) return Promise.resolve(null);
        return caches.open(MEDIA_CACHE);
    }

    function isSaved(url) {
        return mediaCache().then(function (cache) {
            if (!cache) return false;
            return cache.match(url, { ignoreSearch: true }).then(Boolean);
        });
    }

    function save(url, title) {
        return fetch(url, { credentials: "same-origin" }).then(function (response) {
            if (!response || !response.ok) throw new Error("download failed");
            return mediaCache().then(function (cache) {
                if (!cache) throw new Error("cache unavailable");
                return cache.put(url, response.clone()).then(function () {
                    upsert({ url: url, title: title, savedAt: Date.now() });
                });
            });
        });
    }

    function drop(url) {
        return mediaCache()
            .then(function (cache) {
                if (cache) return cache.delete(url, { ignoreSearch: true });
            })
            .then(function () {
                remove(url);
            });
    }

    /* --- Video page: the "Save offline" button ------------------------------ */

    function setupToggle(button) {
        var url = button.getAttribute("data-offline-url");
        var title = button.getAttribute("data-offline-title") || document.title;
        var labelSave = button.getAttribute("data-label-save") || "Save offline";
        var labelSaved = button.getAttribute("data-label-saved") || "Saved offline";
        var labelRemove = button.getAttribute("data-label-remove") || "Remove download";
        var labelError =
            button.getAttribute("data-label-error") ||
            "Could not update the offline copy. Check your connection.";

        if (!url || !("caches" in window)) {
            button.style.display = "none";
            return;
        }

        function render(saved) {
            button.setAttribute("data-saved", saved ? "1" : "0");
            button.classList.toggle("is-saved", saved);
            button.textContent = saved ? "✓ " + labelSaved : "⤓ " + labelSave;
            button.title = saved ? labelRemove : labelSave;
        }

        isSaved(url).then(render);

        button.addEventListener("click", function () {
            var saved = button.getAttribute("data-saved") === "1";

            // Removing a saved track throws away data the visitor downloaded on
            // purpose, possibly over a metered connection. Ask first, and do
            // nothing at all if they cancel.
            var ask = saved
                ? window.fazakkirConfirm({
                      title: labelRemove,
                      message: button.getAttribute("data-label-remove-detail") ||
                          "The saved audio will be deleted from this device and " +
                          "will need to be downloaded again to listen offline.",
                      confirmLabel: labelRemove,
                  })
                : Promise.resolve(true);

            ask.then(function (ok) {
                if (!ok) return;
                button.disabled = true;
                var action = saved ? drop(url) : save(url, title);
                return action
                    .then(function () {
                        render(!saved);
                    })
                    .catch(function () {
                        window.alert(labelError);
                    })
                    .then(function () {
                        button.disabled = false;
                    });
            });
        });
    }

    /* --- /offline/ page: the saved-audio list ------------------------------- */

    function renderLibrary(container) {
        var list = readStore();
        var removeLabel = container.getAttribute("data-remove") || "Remove download";
        var removeTitle =
            container.getAttribute("data-remove-title") || "Remove this download?";
        var removeMessage =
            container.getAttribute("data-remove-message") ||
            "The saved audio will be deleted from this device. You can download it again later.";
        container.innerHTML = "";

        if (!list.length) {
            var empty = document.createElement("p");
            empty.className = "offline-empty";
            empty.textContent =
                container.getAttribute("data-empty") || "You have not saved any audio yet.";
            container.appendChild(empty);
            return;
        }

        list.forEach(function (item) {
            var card = document.createElement("div");
            card.className = "offline-item";

            var head = document.createElement("div");
            head.className = "offline-item-head";

            var title = document.createElement("span");
            title.className = "offline-item-title";
            title.textContent = item.title || "Audio";
            head.appendChild(title);

            var removeBtn = document.createElement("button");
            removeBtn.type = "button";
            removeBtn.className = "offline-remove";
            removeBtn.textContent = "✕";
            removeBtn.title = removeLabel;
            removeBtn.setAttribute("aria-label", removeLabel);
            removeBtn.addEventListener("click", function () {
                // Never destroy a download without an explicit yes.
                window.fazakkirConfirm({
                    title: removeTitle,
                    message: removeMessage + "\n\n" + (item.title || ""),
                    confirmLabel: removeLabel,
                }).then(function (ok) {
                    if (!ok) return;
                    removeBtn.disabled = true;
                    return drop(item.url)
                        .then(function () {
                            renderLibrary(container);
                        })
                        .then(function () {
                            removeBtn.disabled = false;
                        });
                });
            });
            head.appendChild(removeBtn);

            var audio = document.createElement("audio");
            audio.controls = true;
            audio.preload = "metadata";
            audio.src = item.url;

            card.appendChild(head);
            card.appendChild(audio);
            container.appendChild(card);
        });
    }

    function init() {
        if ("serviceWorker" in navigator) {
            navigator.serviceWorker.register("/sw.js").catch(function () {
                /* offline support is unavailable (e.g. insecure origin) */
            });
        }

        Array.prototype.forEach.call(
            document.querySelectorAll("[data-offline-url]"),
            setupToggle
        );

        var library = document.getElementById("offlineList");
        if (library) renderLibrary(library);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
