document.addEventListener("DOMContentLoaded", function() {
    const toggle = document.getElementById("sidebar-toggle");
    const overlay = document.getElementById("sidebar-overlay");
    const themeToggle = document.getElementById("theme-toggle");

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
