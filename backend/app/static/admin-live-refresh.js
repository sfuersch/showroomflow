(() => {
    "use strict";

    const selector = "[data-live-refresh][id]";
    const normalDelay = 7000;
    const maximumDelay = 60000;
    let retryDelay = normalDelay;
    let timer = null;
    let requestRunning = false;

    if (!document.querySelector(selector)) {
        return;
    }

    function schedule(delay = retryDelay) {
        window.clearTimeout(timer);
        timer = window.setTimeout(refresh, delay);
    }

    async function refresh() {
        if (requestRunning || document.hidden) {
            schedule(normalDelay);
            return;
        }

        requestRunning = true;
        const targets = Array.from(document.querySelectorAll(selector));
        targets.forEach((target) => target.setAttribute("aria-busy", "true"));

        try {
            const response = await window.fetch(window.location.href, {
                method: "GET",
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    Accept: "text/html",
                    "X-ShowroomFlow-Live-Refresh": "1",
                },
            });
            if (!response.ok || response.redirected) {
                throw new Error(`Live-Aktualisierung fehlgeschlagen: ${response.status}`);
            }

            const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
            targets.forEach((target) => {
                const replacement = parsed.getElementById(target.id);
                if (!replacement) {
                    return;
                }
                if (replacement.dataset.liveVersion === target.dataset.liveVersion) {
                    target.removeAttribute("aria-busy");
                    return;
                }
                target.replaceWith(document.importNode(replacement, true));
            });
            retryDelay = normalDelay;
        } catch (_error) {
            targets.forEach((target) => target.removeAttribute("aria-busy"));
            retryDelay = Math.min(retryDelay * 2, maximumDelay);
        } finally {
            requestRunning = false;
            schedule();
        }
    }

    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
            retryDelay = normalDelay;
            schedule(250);
        }
    });

    schedule(2500);
})();
