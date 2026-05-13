document.addEventListener("DOMContentLoaded", function() {
    const introScreen = document.getElementById("intro-screen");
    const toggle = document.getElementById("sidebar-toggle");
    const overlay = document.getElementById("sidebar-overlay");
    const themeToggle = document.getElementById("theme-toggle");
    const introStartedAt = Date.now();
    const introStorageKey = "famshareIntroSeen";

    function hideIntroScreen() {
        if (!introScreen) {
            return;
        }

        const minimumVisibleTime = 700;
        const elapsed = Date.now() - introStartedAt;
        const delay = Math.max(0, minimumVisibleTime - elapsed);

        window.setTimeout(function() {
            introScreen.classList.add("intro-slide-up");
            document.body.classList.remove("intro-lock");
            sessionStorage.setItem(introStorageKey, "1");

            window.setTimeout(function() {
                introScreen.remove();
            }, 900);
        }, delay);
    }

    if (introScreen && sessionStorage.getItem(introStorageKey) === "1") {
        introScreen.remove();
        document.body.classList.remove("intro-lock");
    } else if (document.readyState === "complete") {
        hideIntroScreen();
    } else {
        window.addEventListener("load", hideIntroScreen, { once: true });
        window.setTimeout(hideIntroScreen, 3000);
    }

    function setSidebarState() {
        if (!toggle) {
            return;
        }
        const isClosed = document.body.classList.contains("sidebar-closed");
        toggle.setAttribute("aria-expanded", String(!isClosed));
        toggle.setAttribute("aria-label", isClosed ? "Open menu" : "Close menu");
        toggle.textContent = isClosed ? ">" : "<";
    }

    if (toggle) {
        toggle.addEventListener("click", function() {
            document.body.classList.toggle("sidebar-closed");
            setSidebarState();
        });
        setSidebarState();
    }

    if (overlay) {
        overlay.addEventListener("click", function() {
            document.body.classList.add("sidebar-closed");
            setSidebarState();
        });
    }

    if (themeToggle) {
        const storedTheme = localStorage.getItem("theme");
        if (storedTheme === "dark") {
            document.body.classList.add("dark-mode");
        }

        function setThemeLabel() {
            themeToggle.textContent = document.body.classList.contains("dark-mode")
                ? "Light Mode"
                : "Dark Mode";
        }

        themeToggle.addEventListener("click", function() {
            document.body.classList.toggle("dark-mode");
            localStorage.setItem(
                "theme",
                document.body.classList.contains("dark-mode") ? "dark" : "light"
            );
            setThemeLabel();
        });
        setThemeLabel();
    }
});
