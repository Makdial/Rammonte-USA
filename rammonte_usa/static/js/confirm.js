/*
 * A confirmation dialog for destructive actions.
 *
 * window.confirm() is not usable here: it shows an untranslated English
 * message, it cannot be styled, and on some mobile browsers it is suppressed
 * entirely when the page is not user-initiated. Deleting a video, or a copy of
 * audio that was saved for offline listening, has to be deliberate, so this
 * renders an explicit dialog with Cancel and a destructive action.
 *
 * Usage:
 *
 *     fazakkirConfirm({
 *         title:    "Delete this video?",
 *         message:  "This also removes the MP3 from Cloudflare storage.",
 *         confirmLabel: "Delete",
 *         danger:   true,
 *     }).then(function (ok) {
 *         if (ok) { /* go ahead *\/ }
 *     });
 */
(function () {
    "use strict";

    var activeResolver = null;
    var lastFocused = null;

    function build(options) {
        var backdrop = document.createElement("div");
        backdrop.className = "confirm-backdrop";
        backdrop.setAttribute("role", "presentation");

        var dialog = document.createElement("div");
        dialog.className = "confirm-dialog";
        dialog.setAttribute("role", "alertdialog");
        dialog.setAttribute("aria-modal", "true");
        dialog.setAttribute("aria-labelledby", "confirmTitle");
        dialog.setAttribute("aria-describedby", "confirmMessage");

        var heading = document.createElement("h2");
        heading.id = "confirmTitle";
        heading.className = "confirm-title";
        heading.textContent = options.title || "Are you sure?";
        dialog.appendChild(heading);

        var body = document.createElement("p");
        body.id = "confirmMessage";
        body.className = "confirm-message";
        // textContent, never innerHTML: the title may include a video name.
        body.textContent = options.message || "";
        if (!options.message) body.remove();
        dialog.appendChild(body);

        var actions = document.createElement("div");
        actions.className = "confirm-actions";

        var cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "confirm-cancel";
        cancel.textContent = options.cancelLabel || "Cancel";
        actions.appendChild(cancel);

        var confirm = document.createElement("button");
        confirm.type = "button";
        confirm.className = "confirm-accept";
        if (options.danger !== false) confirm.classList.add("is-danger");
        confirm.textContent = options.confirmLabel || "Delete";
        actions.appendChild(confirm);

        dialog.appendChild(actions);
        backdrop.appendChild(dialog);
        return {
            root: backdrop,
            cancel: cancel,
            confirm: confirm,
        };
    }

    function close(result) {
        var resolver = activeResolver;
        activeResolver = null;
        if (resolver) resolver(result);
    }

    function onKeydown(event) {
        if (!activeResolver) return;
        var dialog = document.querySelector(".confirm-dialog");
        if (!dialog) return;

        if (event.key === "Escape") {
            // Cancelling must leave everything exactly as it was.
            event.preventDefault();
            close(false);
            return;
        }

        // Keep the keyboard inside the dialog while it is open.
        if (event.key === "Tab") {
            var focusable = dialog.querySelectorAll("button");
            if (!focusable.length) return;
            var first = focusable[0];
            var last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        }
    }

    window.fazakkirConfirm = function (options) {
        // A second request while one is open resolves the first as cancelled
        // rather than stacking dialogs on top of each other.
        if (activeResolver) close(false);

        options = options || {};
        lastFocused = document.activeElement;

        var parts = build(options);
        document.body.appendChild(parts.root);
        // Let the browser paint the fade-in before moving focus.
        requestAnimationFrame(function () {
            parts.root.classList.add("is-open");
            parts.confirm.focus();
        });

        function finish(result) {
            document.removeEventListener("keydown", onKeydown);
            if (parts.root.parentNode) {
                parts.root.parentNode.removeChild(parts.root);
            }
            if (lastFocused && lastFocused.focus) {
                lastFocused.focus();
            }
            close(result);
        }

        parts.cancel.addEventListener("click", function () {
            finish(false);
        });

        parts.confirm.addEventListener("click", function () {
            finish(true);
        });

        // Clicking the backdrop counts as cancelling, never as confirming.
        parts.root.addEventListener("click", function (event) {
            if (event.target === parts.root) finish(false);
        });

        document.addEventListener("keydown", onKeydown);

        return new Promise(function (resolve) {
            activeResolver = resolve;
        });
    };
})();