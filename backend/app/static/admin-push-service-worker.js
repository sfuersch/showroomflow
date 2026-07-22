self.addEventListener("push", (event) => {
    let payload = {};
    try {
        payload = event.data ? event.data.json() : {};
    } catch (_) {
        payload = {};
    }
    event.waitUntil(self.registration.showNotification(
        payload.title || "ShowroomFlow Qualitätsprüfung",
        {
            body: payload.body || "Ein neues Bild wartet auf Prüfung.",
            tag: payload.tag || "showroomflow-quality-review",
            data: { url: payload.url || "/admin/quality-reviews" }
        }
    ));
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const targetUrl = new URL(
        event.notification.data?.url || "/admin/quality-reviews",
        self.location.origin
    ).href;
    event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(
        async (windowClients) => {
            const existing = windowClients.find((client) => client.url.startsWith(self.location.origin + "/admin/"));
            if (existing) {
                await existing.navigate(targetUrl);
                return existing.focus();
            }
            return clients.openWindow(targetUrl);
        }
    ));
});
