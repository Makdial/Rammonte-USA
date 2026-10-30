/**
 * Mobile navigation menu.
 *
 * On small screens the navbar actions are hidden behind a hamburger button.
 * This toggles that menu and keeps it accessible (aria-expanded, Escape,
 * outside click, and reset when the viewport grows back to desktop).
 */
(function () {
    "use strict";

    var toggle = document.getElementById("navToggle");
    var menu = document.getElementById("navMenu");
    if (!toggle || !menu) return;

    var MOBILE_BREAKPOINT = 768;

    function setOpen(open) {
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        menu.classList.toggle("open", open);
    }

    function isOpen() {
        return toggle.getAttribute("aria-expanded") === "true";
    }

    toggle.addEventListener("click", function (event) {
        event.stopPropagation();
        setOpen(!isOpen());
    });

    // Close when tapping outside the menu.
    document.addEventListener("click", function (event) {
        if (!isOpen()) return;
        if (menu.contains(event.target) || toggle.contains(event.target)) return;
        setOpen(false);
    });

    // Close on Escape.
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && isOpen()) {
            setOpen(false);
            toggle.focus();
        }
    });

    // Reset to the desktop layout when the window gets wide enough.
    window.addEventListener("resize", function () {
        if (window.innerWidth > MOBILE_BREAKPOINT && isOpen()) {
            setOpen(false);
        }
    });
})();
