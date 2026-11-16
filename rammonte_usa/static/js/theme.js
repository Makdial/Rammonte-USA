(function () {
    "use strict";

    var STORAGE_KEY = "fazakkir-theme";
    var root = document.documentElement;

    function currentTheme() {
        return root.getAttribute("data-theme") || "dark";
    }

    function applyTheme(theme) {
        root.setAttribute("data-theme", theme);
        var meta = document.getElementById("themeColor");
        if (meta) {
            meta.setAttribute("content", theme === "light" ? "#f5f6ff" : "#0b0d1a");
        }
        try {
            localStorage.setItem(STORAGE_KEY, theme);
        } catch (e) {
            /* storage unavailable - theme still applies for this page */
        }
    }

    var toggle = document.getElementById("theme-toggle");
    if (toggle) {
        toggle.addEventListener("click", function () {
            applyTheme(currentTheme() === "dark" ? "light" : "dark");
        });
    }
})();
