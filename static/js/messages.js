(function() {
    "use strict";

    function initIcons() {
        if (window.lucide && typeof window.lucide.createIcons === "function") {
            window.lucide.createIcons();
        }
    }

    function initRevealAnimations() {
        var items = document.querySelectorAll(".reveal-item");
        if (!items.length) {
            return;
        }

        if (!("IntersectionObserver" in window)) {
            items.forEach(function(item) {
                item.classList.add("is-visible");
            });
            return;
        }

        var observer = new IntersectionObserver(
            function(entries) {
                entries.forEach(function(entry) {
                    if (entry.isIntersecting) {
                        entry.target.classList.add("is-visible");
                        observer.unobserve(entry.target);
                    }
                });
            },
            {
                root: null,
                threshold: 0.08,
                rootMargin: "0px 0px -24px 0px"
            }
        );

        items.forEach(function(item, index) {
            item.style.transitionDelay = Math.min(index * 35, 260) + "ms";
            observer.observe(item);
        });
    }

    function initSearchFilter() {
        var input = document.getElementById("dm-search");
        var conversations = Array.prototype.slice.call(document.querySelectorAll(".conversation-item"));

        if (!input || !conversations.length) {
            return;
        }

        input.addEventListener("input", function() {
            var query = input.value.trim().toLowerCase();

            conversations.forEach(function(item) {
                var text = item.textContent.toLowerCase();
                var matches = !query || text.indexOf(query) !== -1;
                item.hidden = !matches;
            });
        });
    }

    function initTouchFeedback() {
        var pressables = document.querySelectorAll(".conversation-item, .note-item, .bottom-nav a, .icon-btn, .camera-btn");

        pressables.forEach(function(item) {
            item.addEventListener("pointerdown", function() {
                item.classList.add("is-pressed");
            });

            ["pointerup", "pointercancel", "pointerleave"].forEach(function(eventName) {
                item.addEventListener(eventName, function() {
                    item.classList.remove("is-pressed");
                });
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function() {
        initIcons();
        initRevealAnimations();
        initSearchFilter();
        initTouchFeedback();
    });
})();
