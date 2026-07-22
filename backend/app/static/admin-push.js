(() => {
    const root = document.querySelector("#quality-push-settings");
    if (!root) return;

    const enableButton = root.querySelector("[data-push-enable]");
    const disableButton = root.querySelector("[data-push-disable]");
    const status = root.querySelector("[data-push-status]");
    const configured = root.dataset.enabled === "true";

    const setState = (active, message) => {
        enableButton.hidden = active;
        disableButton.hidden = !active;
        status.textContent = message;
    };

    const applicationServerKey = (value) => {
        const padding = "=".repeat((4 - value.length % 4) % 4);
        const binary = atob((value + padding).replace(/-/g, "+").replace(/_/g, "/"));
        return Uint8Array.from(binary, (character) => character.charCodeAt(0));
    };

    const save = async (subscription) => {
        const json = subscription.toJSON();
        const response = await fetch("/admin/push-subscriptions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                csrf_token: root.dataset.csrfToken,
                endpoint: subscription.endpoint,
                keys: json.keys
            })
        });
        if (!response.ok) throw new Error("subscription-save-failed");
    };

    const registration = async () => navigator.serviceWorker.register(
        "/admin/push-service-worker.js",
        { scope: "/admin/" }
    );

    const refresh = async () => {
        if (!configured) {
            enableButton.disabled = true;
            setState(false, "Web-Push ist auf dem Server noch nicht konfiguriert.");
            return;
        }
        if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
            enableButton.disabled = true;
            setState(false, "Dieser Browser unterstützt keine Web-Push-Benachrichtigungen.");
            return;
        }
        const worker = await registration();
        const subscription = await worker.pushManager.getSubscription();
        if (subscription) {
            await save(subscription);
            setState(true, "Benachrichtigungen sind auf diesem Gerät aktiv.");
        } else if (Notification.permission === "denied") {
            enableButton.disabled = true;
            setState(false, "Benachrichtigungen wurden im Browser blockiert.");
        } else {
            setState(false, "Benachrichtigungen sind auf diesem Gerät noch nicht aktiv.");
        }
    };

    enableButton.addEventListener("click", async () => {
        enableButton.disabled = true;
        status.textContent = "Benachrichtigungen werden aktiviert …";
        try {
            const worker = await registration();
            const permission = await Notification.requestPermission();
            if (permission !== "granted") throw new Error("permission-denied");
            const subscription = await worker.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: applicationServerKey(root.dataset.publicKey)
            });
            await save(subscription);
            setState(true, "Benachrichtigungen sind auf diesem Gerät aktiv.");
        } catch (_) {
            setState(false, "Aktivierung fehlgeschlagen. Bitte Browser-Einstellungen prüfen.");
        } finally {
            enableButton.disabled = false;
        }
    });

    disableButton.addEventListener("click", async () => {
        disableButton.disabled = true;
        try {
            const worker = await registration();
            const subscription = await worker.pushManager.getSubscription();
            if (subscription) {
                await fetch("/admin/push-subscriptions/remove", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        csrf_token: root.dataset.csrfToken,
                        endpoint: subscription.endpoint
                    })
                });
                await subscription.unsubscribe();
            }
            setState(false, "Benachrichtigungen sind auf diesem Gerät deaktiviert.");
        } catch (_) {
            status.textContent = "Deaktivierung fehlgeschlagen. Bitte erneut versuchen.";
        } finally {
            disableButton.disabled = false;
        }
    });

    refresh().catch(() => setState(false, "Benachrichtigungsstatus konnte nicht geladen werden."));
})();
