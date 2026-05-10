window.addEventListener("load", function () {
    const intro = document.getElementById("intro-screen");
    const mainContent = document.getElementById("main-content");
    const toggleBtn = document.getElementById("theme-toggle");
    const sidebarToggle = document.getElementById("sidebar-toggle");
    const sidebarOverlay = document.getElementById("sidebar-overlay");

    function setTheme(theme) {
        const isDark = theme === "dark";
        document.body.classList.toggle("dark-mode", isDark);
        if (toggleBtn) {
            toggleBtn.textContent = isDark ? "Light Mode" : "Dark Mode";
        }
    }

    setTheme(localStorage.getItem("famshare_theme") || "light");

    if (toggleBtn) {
        toggleBtn.addEventListener("click", function () {
            const nextTheme = document.body.classList.contains("dark-mode") ? "light" : "dark";
            localStorage.setItem("famshare_theme", nextTheme);
            setTheme(nextTheme);
        });
    }

    function setSidebarOpen(isOpen) {
        document.body.classList.toggle("sidebar-closed", !isOpen);
        if (sidebarToggle) {
            sidebarToggle.textContent = isOpen ? "\u2039" : "\u203a";
            sidebarToggle.setAttribute("aria-expanded", String(isOpen));
            sidebarToggle.setAttribute("aria-label", isOpen ? "Close menu" : "Open menu");
        }
        localStorage.setItem("famshare_sidebar_open", String(isOpen));
    }

    if (sidebarToggle) {
        const savedSidebarState = localStorage.getItem("famshare_sidebar_open");
        const startsOpen = savedSidebarState === null
            ? !window.matchMedia("(max-width: 900px)").matches
            : savedSidebarState !== "false";
        setSidebarOpen(startsOpen);

        sidebarToggle.addEventListener("click", function () {
            setSidebarOpen(document.body.classList.contains("sidebar-closed"));
        });
    }

    if (sidebarOverlay) {
        sidebarOverlay.addEventListener("click", function () {
            setSidebarOpen(false);
        });
    }

    const appSidebar = document.getElementById("app-sidebar");
    if (appSidebar) {
        appSidebar.addEventListener("click", function (event) {
            if (!window.matchMedia("(max-width: 900px)").matches) {
                return;
            }
            const clickedItem = event.target.closest("a, button");
            if (!clickedItem) {
                return;
            }
            if (clickedItem.id === "sidebar-toggle") {
                return;
            }
            setSidebarOpen(false);
        });
    }

    function executeInlineScripts(root) {
        root.querySelectorAll("script").forEach(function (oldScript) {
            const script = document.createElement("script");
            Array.from(oldScript.attributes).forEach(function (attr) {
                script.setAttribute(attr.name, attr.value);
            });
            if (oldScript.src) {
                script.src = oldScript.src;
                script.async = false;
            } else {
                script.textContent = oldScript.textContent;
            }
            oldScript.parentNode.replaceChild(script, oldScript);
        });
    }

    function bindDynamicPageHandlers(root) {
        if (!root) {
            root = document;
        }

        const profilePhotoInput = root.querySelector("#profile-photo-input");
        const profilePhotoPreview = root.querySelector("#profile-photo-preview");
        const removeProfilePhotoBtn = root.querySelector("#remove-profile-photo");
        const removeProfilePhotoInput = root.querySelector("#remove-profile-photo-input");

        if (profilePhotoInput && profilePhotoPreview && removeProfilePhotoInput && !profilePhotoInput.dataset.ajaxBound) {
            profilePhotoInput.dataset.ajaxBound = "1";
            profilePhotoInput.addEventListener("change", function () {
                const file = profilePhotoInput.files && profilePhotoInput.files[0];
                removeProfilePhotoInput.value = "0";
                if (file) {
                    profilePhotoPreview.src = URL.createObjectURL(file);
                }
            });
        }

        if (removeProfilePhotoBtn && profilePhotoPreview && removeProfilePhotoInput && profilePhotoInput && !removeProfilePhotoBtn.dataset.ajaxBound) {
            removeProfilePhotoBtn.dataset.ajaxBound = "1";
            removeProfilePhotoBtn.addEventListener("click", function () {
                profilePhotoInput.value = "";
                removeProfilePhotoInput.value = "1";
                profilePhotoPreview.src = profilePhotoPreview.dataset.defaultSrc;
            });
        }

        root.querySelectorAll(".emergency-help-btn").forEach(function (button) {
            if (button.dataset.ajaxBound) {
                return;
            }
            button.dataset.ajaxBound = "1";
            button.addEventListener("click", async function () {
                const alertId = button.dataset.alertId;
                await fetch(`/emergency/${alertId}/respond`, {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({message: "I can help."})
                });
                button.textContent = "Responding";
                button.disabled = true;
            });
        });

        root.querySelectorAll(".emergency-safe-btn").forEach(function (button) {
            if (button.dataset.ajaxBound) {
                return;
            }
            button.dataset.ajaxBound = "1";
            button.addEventListener("click", async function () {
                const alertId = button.dataset.alertId;
                await fetch(`/emergency/${alertId}/safe`, {method: "POST"});
                const card = button.closest(".emergency-alert-card");
                if (card) {
                    card.remove();
                }
            });
        });
    }

    function isSameOriginLink(link) {
        try {
            return new URL(link.href, location.href).origin === location.origin;
        } catch (error) {
            return false;
        }
    }

    function shouldHandleSpaLink(link) {
        if (!link || link.target || link.hasAttribute("download") || link.dataset.noSpa) {
            return false;
        }
        try {
            const url = new URL(link.href, location.href);
            if (url.origin !== location.origin) {
                return false;
            }
            const blockedRoutes = ["/logout", "/login", "/register"];
            if (blockedRoutes.some(function (route) { return url.pathname.startsWith(route); })) {
                return false;
            }
            if (url.pathname === location.pathname) {
                return false;
            }
            return true;
        } catch (error) {
            return false;
        }
    }

    async function navigateTo(url, replace) {
        try {
            const response = await fetch(url, {
                method: "GET",
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "text/html"
                },
                credentials: "same-origin"
            });

            if (!response.ok) {
                alert("Could not load page. Please try again.");
                return;
            }

            const html = await response.text();
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, "text/html");
            const newMain = doc.querySelector("main");
            if (!newMain) {
                alert("Could not load page content.");
                return;
            }

            const currentMain = document.querySelector("main");
            if (!currentMain) {
                alert("Could not update page content.");
                return;
            }

            currentMain.replaceWith(newMain);
            document.title = doc.title || document.title;
            const finalUrl = response.url || url;
            if (replace) {
                history.replaceState({url: finalUrl}, "", finalUrl);
            } else {
                history.pushState({url: finalUrl}, "", finalUrl);
            }
            executeInlineScripts(newMain);
            setupAvatarFallbacks();
            bindDynamicPageHandlers(newMain);
            window.scrollTo(0, 0);
        } catch (error) {
            alert("Navigation failed. Please try again.");
        }
    }

    function shouldHandleSpaForm(form) {
        if (!form || form.dataset.noSpa) {
            return false;
        }
        const method = (form.method || "GET").toUpperCase();
        if (method !== "GET" && method !== "POST") {
            return false;
        }
        if (!form.closest("main")) {
            return false;
        }
        const actionUrl = form.action || location.href;
        let url;
        try {
            url = new URL(actionUrl, location.href);
        } catch (error) {
            return false;
        }
        if (url.origin !== location.origin) {
            return false;
        }
        const blockedRoutes = ["/logout", "/login", "/register"];
        if (blockedRoutes.some(function (route) { return url.pathname.startsWith(route); })) {
            return false;
        }
        return true;
    }

    async function submitSpaForm(form) {
        const method = (form.method || "GET").toUpperCase();
        const actionUrl = form.action || location.href;
        const url = new URL(actionUrl, location.href);
        let options = {
            method: method,
            credentials: "same-origin",
            headers: {
                "X-Requested-With": "XMLHttpRequest"
            }
        };
        if (method === "GET") {
            const formData = new FormData(form);
            const params = new URLSearchParams(formData);
            url.search = params.toString();
        } else {
            const formData = new FormData(form);
            options.body = formData;
        }

        const response = await fetch(url.toString(), options);
        const contentType = response.headers.get("Content-Type") || "";
        if (contentType.includes("application/json")) {
            const data = await response.json();
            if (!response.ok || !data.ok) {
                alert(data.error || "Form submission failed.");
                return;
            }
            if (data.redirect) {
                await navigateTo(data.redirect);
                return;
            }
            if (data.html) {
                const parser = new DOMParser();
                const doc = parser.parseFromString(data.html, "text/html");
                const newMain = doc.querySelector("main");
                if (newMain) {
                    const currentMain = document.querySelector("main");
                    currentMain.replaceWith(newMain);
                    document.title = doc.title || document.title;
                    executeInlineScripts(newMain);
                    setupAvatarFallbacks();
                    bindDynamicPageHandlers(newMain);
                    return;
                }
            }
            return;
        }

        if (!response.ok) {
            alert("Form submission failed.");
            return;
        }

        const html = await response.text();
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, "text/html");
        const newMain = doc.querySelector("main");
        if (!newMain) {
            alert("Could not update page content.");
            return;
        }

        const currentMain = document.querySelector("main");
        currentMain.replaceWith(newMain);
        document.title = doc.title || document.title;
        const finalUrl = response.url || url.toString();
        history.pushState({url: finalUrl}, "", finalUrl);
        executeInlineScripts(newMain);
        setupAvatarFallbacks();
        bindDynamicPageHandlers(newMain);
        window.scrollTo(0, 0);
    }

    document.addEventListener("submit", function (event) {
        if (event.defaultPrevented) {
            return;
        }
        const form = event.target;
        if (!shouldHandleSpaForm(form)) {
            return;
        }
        event.preventDefault();
        submitSpaForm(form).catch(function () {
            alert("Could not submit form. Please try again.");
        });
    });

    document.addEventListener("click", function (event) {
        if (event.defaultPrevented) {
            return;
        }
        const link = event.target.closest("a");
        if (!link) {
            return;
        }
        try {
            const url = new URL(link.href, location.href);
            if (url.origin === location.origin && url.pathname === location.pathname && url.hash) {
                const target = document.querySelector(url.hash);
                if (target) {
                    event.preventDefault();
                    target.scrollIntoView({behavior: "smooth"});
                    return;
                }
            }
        } catch (error) {
            // Ignore invalid URLs and continue.
        }
        if (!shouldHandleSpaLink(link)) {
            return;
        }
        event.preventDefault();
        navigateTo(link.href);
    });

    window.addEventListener("popstate", function (event) {
        if (event.state && event.state.url) {
            navigateTo(event.state.url, true);
        }
    });

    history.replaceState({url: location.href}, "", location.href);
    bindDynamicPageHandlers(document);

    document.querySelectorAll(".flash").forEach(function (message) {
        setTimeout(function () {
            message.remove();
        }, 5200);
    });

    function setupAvatarFallbacks() {
        const defaultAvatar = window.DEFAULT_AVATAR_SRC || "/static/img/default-avatar.svg";
        document.querySelectorAll("img.avatar, img.profile-pic-small, img.profile-pic-large, .sidebar-profile img").forEach(function (img) {
            if (!img.dataset.avatarFallbackBound) {
                img.dataset.avatarFallbackBound = "1";
                img.onerror = function () {
                    if (img.src !== defaultAvatar) {
                        img.src = defaultAvatar;
                    }
                };
            }
        });
    }

    setupAvatarFallbacks();

    const profilePhotoInput = document.getElementById("profile-photo-input");
    const profilePhotoPreview = document.getElementById("profile-photo-preview");
    const removeProfilePhotoBtn = document.getElementById("remove-profile-photo");
    const removeProfilePhotoInput = document.getElementById("remove-profile-photo-input");

    if (profilePhotoInput && profilePhotoPreview && removeProfilePhotoInput) {
        profilePhotoInput.addEventListener("change", function () {
            const file = profilePhotoInput.files && profilePhotoInput.files[0];
            removeProfilePhotoInput.value = "0";

            if (file) {
                profilePhotoPreview.src = URL.createObjectURL(file);
            }
        });
    }

    if (removeProfilePhotoBtn && profilePhotoPreview && removeProfilePhotoInput && profilePhotoInput) {
        removeProfilePhotoBtn.addEventListener("click", function () {
            profilePhotoInput.value = "";
            removeProfilePhotoInput.value = "1";
            profilePhotoPreview.src = profilePhotoPreview.dataset.defaultSrc;
        });
    }

    const sosModal = document.getElementById("sos-modal");
    const sosForm = document.getElementById("sos-form");
    const emergencyToast = document.getElementById("emergency-toast");
    const openSosButtons = document.querySelectorAll("#open-sos-modal, [data-open-sos]");
    const closeSosBtn = document.getElementById("close-sos-modal");
    const useCurrentLocationBtn = document.getElementById("use-current-location");
    const sosLocationStatus = document.getElementById("sos-location-status");
    const currentUserId = Number(document.body.dataset.currentUserId || 0);
    const appSocket = window.io ? io({
        transports: ["websocket", "polling"],
        reconnection: true
    }) : null;

    function setSosLocationStatus(message) {
        if (sosLocationStatus) {
            sosLocationStatus.textContent = message;
        }
    }

    function getLocationErrorMessage(error) {
        if (!window.isSecureContext) {
            return "Location needs HTTPS or localhost. Open the app on 127.0.0.1/localhost instead of a network IP, or enable HTTPS.";
        }
        if (!error) {
            return "Could not access current location. Please check browser location settings.";
        }
        if (error.code === error.PERMISSION_DENIED) {
            return "Location permission is blocked. Allow location for this site in the browser, then try again.";
        }
        if (error.code === error.POSITION_UNAVAILABLE) {
            return "Your device could not provide a location right now. Turn on GPS/location services and try again.";
        }
        if (error.code === error.TIMEOUT) {
            return "Location request timed out. Try again near a window or with GPS/location services enabled.";
        }
        return "Could not access current location. Please type the location manually.";
    }

    async function updateSosLocation(showStatus) {
        if (!navigator.geolocation || !sosForm) {
            if (showStatus) {
                setSosLocationStatus("Location access is not available in this browser.");
            }
            return false;
        }

        if (!window.isSecureContext) {
            if (showStatus) {
                setSosLocationStatus(getLocationErrorMessage());
            }
            return false;
        }

        if (showStatus) {
            setSosLocationStatus("Fetching current location...");
        }

        return new Promise(function (resolve) {
            navigator.geolocation.getCurrentPosition(function (position) {
                const latitude = position.coords.latitude;
                const longitude = position.coords.longitude;
                sosForm.elements.latitude.value = latitude;
                sosForm.elements.longitude.value = longitude;
                if (showStatus) {
                    setSosLocationStatus("Current location captured. Address will be added when SOS is sent.");
                }
                if (appSocket) {
                    appSocket.emit("update_user_location", {
                        latitude: latitude,
                        longitude: longitude
                    });
                }
                resolve(true);
            }, function (error) {
                if (showStatus) {
                    setSosLocationStatus(getLocationErrorMessage(error));
                }
                resolve(false);
            }, {
                enableHighAccuracy: true,
                timeout: 15000,
                maximumAge: 0
            });
        });
    }

    function openSosModal() {
        if (sosModal) {
            sosModal.classList.add("show-sos-modal");
            sosModal.setAttribute("aria-hidden", "false");
        }
        updateSosLocation(true);
    }

    function closeSosModal() {
        if (sosModal) {
            sosModal.classList.remove("show-sos-modal");
            sosModal.setAttribute("aria-hidden", "true");
        }
    }

    function playEmergencySound() {
        try {
            const audioContext = new (window.AudioContext || window.webkitAudioContext)();
            const oscillator = audioContext.createOscillator();
            const gain = audioContext.createGain();
            oscillator.type = "sine";
            oscillator.frequency.value = 880;
            gain.gain.value = 0.08;
            oscillator.connect(gain);
            gain.connect(audioContext.destination);
            oscillator.start();
            setTimeout(function () {
                oscillator.stop();
                audioContext.close();
            }, 450);
        } catch (error) {
            return;
        }
    }

    function showEmergencyToast(alert) {
        if (!emergencyToast) {
            return;
        }
        if (currentUserId && Number(alert.user_id) === currentUserId) {
            return;
        }
        emergencyToast.innerHTML = `
            <strong>${alert.emergency_type} · ${alert.severity}</strong>
            <p>${alert.username} needs help${alert.location_text ? " near " + alert.location_text : ""}.</p>
            <a href="/emergency">Open Emergency</a>
        `;
        emergencyToast.classList.add("show-emergency-toast");
        playEmergencySound();
        setTimeout(function () {
            emergencyToast.classList.remove("show-emergency-toast");
        }, 9000);
    }

    function showSosSentToast(data) {
        if (!emergencyToast) {
            return;
        }
        const nearbyUsers = data.nearby_users || [];
        const nearbyText = nearbyUsers.length
            ? `Nearby online users: ${nearbyUsers.map(function (user) { return user.username; }).join(", ")}.`
            : "No nearby online users were found yet. The alert is still visible in the emergency feed.";
        emergencyToast.innerHTML = `
            <strong>SOS sent</strong>
            <p>${nearbyText}</p>
            <a href="/emergency">Open Emergency</a>
        `;
        emergencyToast.classList.add("show-emergency-toast");
        setTimeout(function () {
            emergencyToast.classList.remove("show-emergency-toast");
        }, 9000);
    }

    openSosButtons.forEach(function (button) {
        button.addEventListener("click", openSosModal);
    });

    if (closeSosBtn) {
        closeSosBtn.addEventListener("click", closeSosModal);
    }

    if (sosModal) {
        sosModal.addEventListener("click", function (event) {
            if (event.target === sosModal) {
                closeSosModal();
            }
        });
    }

    if (useCurrentLocationBtn) {
        useCurrentLocationBtn.addEventListener("click", function () {
            updateSosLocation(true);
        });
    }

    if (sosForm) {
        sosForm.addEventListener("submit", async function (event) {
            event.preventDefault();
            const button = sosForm.querySelector("button[type='submit']");
            button.disabled = true;
            try {
                await updateSosLocation(true);
                const formData = new FormData(sosForm);
                const payload = Object.fromEntries(formData.entries());
                const response = await fetch("/emergency/create", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify(payload)
                });
                const data = await response.json();
                if (!response.ok || !data.ok) {
                    throw new Error(data.error || "Could not send SOS.");
                }
                closeSosModal();
                showSosSentToast(data);
            } catch (error) {
                alert(error.message);
            } finally {
                button.disabled = false;
            }
        });
    }

    document.querySelectorAll(".emergency-help-btn").forEach(function (button) {
        button.addEventListener("click", async function () {
            const alertId = button.dataset.alertId;
            await fetch(`/emergency/${alertId}/respond`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({message: "I can help."})
            });
            button.textContent = "Responding";
            button.disabled = true;
        });
    });

    document.querySelectorAll(".emergency-safe-btn").forEach(function (button) {
        button.addEventListener("click", async function () {
            const alertId = button.dataset.alertId;
            await fetch(`/emergency/${alertId}/safe`, {method: "POST"});
            const card = button.closest(".emergency-alert-card");
            if (card) {
                card.remove();
            }
        });
    });

    if (appSocket) {
        appSocket.on("emergency_alert", showEmergencyToast);
        appSocket.on("emergency_created", showSosSentToast);
        document.querySelectorAll("[data-alert-id]").forEach(function (node) {
            appSocket.emit("join_emergency", {alert_id: node.dataset.alertId});
        });
        appSocket.on("emergency_safe", function (data) {
            const card = document.querySelector(`.emergency-alert-card[data-alert-id="${data.alert_id}"]`);
            if (card) {
                card.remove();
            }
        });
        appSocket.on("emergency_chat_message", function (data) {
            const box = document.getElementById("emergency-chat-box");
            if (!box || Number(box.dataset.alertId) !== Number(data.alert_id)) {
                return;
            }
            const row = document.createElement("div");
            row.className = "emergency-chat-message";
            row.innerHTML = `<strong>${data.username}</strong><p>${data.message_text}</p>`;
            box.appendChild(row);
            box.scrollTop = box.scrollHeight;
        });
    }

    if (!intro || !mainContent) {
        return;
    }

    if (localStorage.getItem("famshare_intro_seen") === "true") {
        intro.style.display = "none";
        mainContent.classList.remove("hidden-content");
        mainContent.classList.add("show-content");
        return;
    }

    localStorage.setItem("famshare_intro_seen", "true");

    setTimeout(() => {
        intro.classList.add("intro-slide-up");
    }, 1100);

    setTimeout(() => {
        intro.style.display = "none";
        mainContent.classList.remove("hidden-content");
        mainContent.classList.add("show-content");
    }, 2100);
});
