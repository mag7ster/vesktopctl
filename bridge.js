"use strict";

// Loaded by the Vencord desktop main bundle in Electron's main process.
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const { BrowserWindow, app } = require("electron");

// Flatpak changes XDG_RUNTIME_DIR to an app-private directory. The override in
// README exposes this specific host runtime subdirectory at its host path.
const runtime = process.env.FLATPAK_ID
    ? path.join("/run/user", String(process.getuid()))
    : process.env.XDG_RUNTIME_DIR;
if (!runtime || !path.isAbsolute(runtime)) throw new Error("XDG_RUNTIME_DIR is missing or invalid");
const directory = path.join(runtime, "vesktopctl");
const address = path.join(directory, "control.sock");

if (!globalThis.__vesktopctlServer) {
    let queue = Promise.resolve();
    let ownsSocket = false;

    async function click(action) {
        // Keep VCC's renderer click mechanism. IDs from its original lottie
        // selectors are a fallback for Discord builds without stable labels.
        const selector = action === "mute"
            ? 'button[aria-label="Mute"], button[aria-label="Unmute"], button:has(g[clip-path="url(#__lottie_element_5)"])'
            : 'button[aria-label="Deafen"], button[aria-label="Undeafen"], button:has(g[clip-path="url(#__lottie_element_42)"])';
        const script = `(() => { const button = document.querySelector(${JSON.stringify(selector)}); if (!button) return false; button.click(); return true; })()`;

        for (const window of BrowserWindow.getAllWindows()) {
            if (window.isDestroyed() || window.webContents.isDestroyed() || window.webContents.isLoading()) continue;
            try {
                if (await window.webContents.executeJavaScript(script)) return;
            } catch (error) {
                console.warn("[vesktopctl] renderer unavailable:", error);
            }
        }
        throw new Error("Discord mute/deafen button not found; open the Discord window");
    }

    const server = net.createServer(connection => {
        connection.setEncoding("utf8");
        connection.setTimeout(5000);
        let input = "";
        let handled = false;
        connection.on("timeout", () => connection.destroy());
        connection.on("error", error => console.warn("[vesktopctl] client error:", error));
        connection.on("data", chunk => {
            if (handled) return;
            input += chunk;
            if (input.length > 32) {
                handled = true;
                connection.end("ERR command too long\n");
                return;
            }
            const newline = input.indexOf("\n");
            if (newline < 0) return;
            handled = true;
            connection.setTimeout(0); // Waiting in the serial queue is valid activity.
            const command = input.slice(0, newline);
            if (newline !== input.length - 1 || !["mute", "deafen"].includes(command)) {
                connection.end("ERR invalid command\n");
                return;
            }
            const work = queue.then(() => click(command));
            queue = work.catch(() => {});
            work.then(
                () => connection.end("OK\n"),
                error => connection.end(`ERR ${String(error.message).replace(/[\r\n]/g, " ")}\n`)
            );
        });
    });

    server.on("error", error => console.error("[vesktopctl] socket server:", error));
    globalThis.__vesktopctlServer = server;

    async function listen() {
        fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
        const stat = fs.statSync(directory);
        if (stat.uid !== process.getuid() || (stat.mode & 0o077)) {
            throw new Error(`unsafe socket directory: ${directory}`);
        }
        try {
            const existing = fs.lstatSync(address);
            if (!existing.isSocket()) throw new Error(`socket path occupied: ${address}`);
            // Probe before removing a stale socket. Never remove a live server.
            await new Promise((resolve, reject) => {
                const probe = net.connect(address);
                probe.once("connect", () => { probe.destroy(); reject(new Error("another vesktopctl server is running")); });
                probe.once("error", error => error.code === "ECONNREFUSED" ? resolve() : reject(error));
            });
            fs.unlinkSync(address);
        } catch (error) {
            if (error.code !== "ENOENT") throw error;
        }
        server.listen(address, () => {
            ownsSocket = true;
            try {
                fs.chmodSync(address, 0o600);
                console.info(`[vesktopctl] listening on ${address}`);
            } catch (error) {
                console.error("[vesktopctl] cannot secure socket:", error);
                server.close();
                try { fs.unlinkSync(address); } catch {}
                ownsSocket = false;
            }
        });
    }

    listen().catch(error => console.error("[vesktopctl] startup failed:", error));
    app.once("before-quit", () => {
        if (!ownsSocket) return;
        server.close();
        try { fs.unlinkSync(address); } catch (error) { if (error.code !== "ENOENT") console.warn("[vesktopctl] cleanup:", error); }
    });
}
