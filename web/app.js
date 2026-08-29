function saveToken() {
    const value = document.getElementById("tokenInput").value.trim();
    if (!value) return;

    localStorage.setItem("token", value);
    document.getElementById("loginBox").style.display = "none";
    tasks();
}

function logout() {
    localStorage.removeItem("token");
    document.getElementById("loginBox").style.display = "block";
}

function checkLogin() {
    if (localStorage.getItem("token")) {
        document.getElementById("loginBox").style.display = "none";
    }
}

function headers() {
    return {
        "Authorization": "Bearer " + (localStorage.getItem("token") || "")
    };
}

function clearInput() {
    document.getElementById("links").value = "";
}

async function addTracks() {
    const text = document.getElementById("links").value;
    const links = text.split("\n").map(x => x.trim()).filter(Boolean);
    const result = document.getElementById("addResult");

    try {
        const r = await fetch("/api/add", {
            method: "POST",
            headers: {
                ...headers(),
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ links })
        });

        const data = await r.json();

        // Server-generated data only (track ids), but textContent is used
        // throughout this file on principle: never assign untrusted or
        // dynamic values via innerHTML. See tasks() below for why.
        result.textContent = JSON.stringify(data);

        tasks();

    } catch (e) {
        result.textContent = e.message;
    }
}

async function health() {
    const box = document.getElementById("health");

    try {
        const r = await fetch("/health");
        const data = await r.json();

        const rows = [
            ["Status", data.status],
            ["Database", data.database],
            ["Library", data.library],
            ["Workers", data.workers],
            ["Queue", data.queue_size],
        ];

        box.replaceChildren();

        for (const [label, value] of rows) {
            const row = document.createElement("div");
            row.className = "status";
            row.textContent = `${label}: ${value}`;
            box.appendChild(row);
        }

    } catch (e) {
        box.replaceChildren();
        box.textContent = "Offline";
    }
}

async function tasks() {
    try {
        const r = await fetch("/api/tasks", { headers: headers() });
        if (!r.ok) throw Error();

        const data = await r.json();

        document.getElementById("queueCount").textContent = data.length + " tracks";

        const box = document.getElementById("tasks");
        box.replaceChildren();

        for (const t of data) {
            // t.title / t.artist / t.url are derived from YouTube video
            // metadata, which is attacker-controlled (anyone can upload a
            // video with a hostile title). They must only ever be inserted
            // via textContent/DOM APIs below - never innerHTML - or a
            // malicious title becomes script execution in this page,
            // which can read the API token out of localStorage.
            const card = document.createElement("div");
            card.className = "track";

            const cover = document.createElement("div");
            cover.className = "cover";
            cover.textContent = "🎵";

            const info = document.createElement("div");
            info.className = "track-info";

            const title = document.createElement("div");
            title.className = "track-title";
            title.textContent = t.title || "Processing...";

            const artist = document.createElement("div");
            artist.className = "track-artist";
            artist.textContent = t.artist || "";

            const url = document.createElement("small");
            url.textContent = t.url || "";

            info.appendChild(title);
            info.appendChild(artist);
            info.appendChild(url);

            const status = document.createElement("div");
            status.className = "track-status";
            if (t.status) status.classList.add(t.status);
            status.textContent = t.status || "";

            card.appendChild(cover);
            card.appendChild(info);
            card.appendChild(status);

            box.appendChild(card);
        }

    } catch (e) {
        // Polling loop - a transient network hiccup shouldn't throw to console.
    }
}

setInterval(() => {
    health();
    tasks();
}, 3000);

checkLogin();
health();
tasks();
setTimeout(() => library(), 0);

// ---------- Library: search and delete ----------
// Deleting is the one destructive thing this UI can do, so it asks first and
// the server moves the file to a trash folder rather than unlinking it.

let librarySearchTimer = null;

function scheduleLibrarySearch() {
    clearTimeout(librarySearchTimer);
    librarySearchTimer = setTimeout(library, 300);
}

async function library() {
    const box = document.getElementById("library");
    const count = document.getElementById("libraryCount");
    const q = document.getElementById("librarySearch").value.trim();
    try {
        const r = await fetch("/api/library?q=" + encodeURIComponent(q), { headers: headers() });
        if (!r.ok) return;
        const data = await r.json();
        count.textContent = data.length + (data.length === 200 ? "+" : "");
        box.replaceChildren();

        for (const t of data) {
            const card = document.createElement("div");
            card.className = "track";

            const info = document.createElement("div");
            info.className = "track-info";

            const title = document.createElement("div");
            title.className = "track-title";
            title.textContent = (t.track ? t.track + ". " : "") + t.title;

            const artist = document.createElement("div");
            artist.className = "track-artist";
            artist.textContent = t.artist;

            const album = document.createElement("small");
            album.textContent = t.album;

            info.appendChild(title);
            info.appendChild(artist);
            info.appendChild(album);

            const del = document.createElement("button");
            del.className = "secondary";
            del.textContent = "Delete";
            del.onclick = () => removeTrack(t, del);

            card.appendChild(info);
            card.appendChild(del);
            box.appendChild(card);
        }
    } catch (e) {
        // Transient network hiccup - the next search will retry.
    }
}

async function removeTrack(track, button) {
    if (!confirm(`Delete "${track.title}" by ${track.artist}?\n\nThe file moves to the trash folder on the laptop.`)) return;
    button.disabled = true;
    button.textContent = "…";
    try {
        const r = await fetch("/api/library", {
            method: "DELETE",
            headers: { ...headers(), "Content-Type": "application/json" },
            body: JSON.stringify({ path: track.path }),
        });
        if (r.ok) {
            button.closest(".track").remove();
        } else {
            const err = await r.json().catch(() => ({}));
            button.disabled = false;
            button.textContent = "Delete";
            alert("Could not delete: " + (err.detail || r.status));
        }
    } catch (e) {
        button.disabled = false;
        button.textContent = "Delete";
        alert("Could not delete: " + e.message);
    }
}
