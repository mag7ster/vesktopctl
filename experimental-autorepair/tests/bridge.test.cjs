const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const lifecycle = require("../autorepair.js");
const source = fs.readFileSync(path.resolve(__dirname, "../bridge.js"), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));

function loadBridge(t, { failListen = false, windows = [], missingRuntime = false } = {}) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vcc-bridge-"));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const bridgeDir = path.join(dir, "vesktopctl-experimental");
    fs.mkdirSync(bridgeDir);
    fs.copyFileSync(path.resolve(__dirname, "../injection.js"), path.join(bridgeDir, "injection.js"));
    const bundle = path.join(dir, "vencordDesktopMain.js");
    fs.writeFileSync(bundle, "globalThis.updated = true;");
    const address = path.join(dir, "vesktopctl/control.sock");
    let socket = false;
    let accepts;
    let starts = 0;
    const server = new EventEmitter();
    server.listen = (_, callback) => {
        starts++;
        if (failListen) server.emit("error", new Error("listen denied"));
        else { socket = true; callback(); }
    };
    server.close = () => { socket = false; };
    const mockFs = Object.create(fs);
    mockFs.lstatSync = filename => filename === address && socket
        ? { isSocket: () => true, ino: 42 }
        : fs.lstatSync(filename);
    mockFs.chmodSync = (filename, mode) => { if (filename !== address) fs.chmodSync(filename, mode); };
    mockFs.unlinkSync = filename => { if (filename === address && socket) socket = false; else fs.unlinkSync(filename); };
    const app = new EventEmitter();
    app.exit = code => code;
    app.relaunch = () => {};
    const messages = [];
    const logger = Object.fromEntries(["log", "info", "warn", "error"].map(name => [name, (...args) => messages.push([name, ...args])]));
    const globals = {};
    const localRequire = name => {
        if (name === "node:fs") return mockFs;
        if (name === "node:net") return { createServer(callback) { accepts = callback; return server; } };
        if (name === "electron") return { app, BrowserWindow: { getAllWindows: () => windows } };
        if (name === "./autorepair.js") return lifecycle;
        return require(name);
    };
    const load = () => {
        const run = vm.runInNewContext(`(function(require, __dirname, process, console, globalThis) {\n${source}\n})`);
        run(localRequire, bridgeDir, { getuid: () => process.getuid(), env: missingRuntime ? {} : { XDG_RUNTIME_DIR: dir } }, logger, globals);
    };
    load();
    function connect(command) {
        const connection = new EventEmitter();
        connection.setEncoding = () => {};
        connection.setTimeout = () => {};
        connection.end = response => { connection.response = response; };
        connection.destroy = () => {};
        accepts(connection);
        if (command !== undefined) connection.emit("data", command);
        return connection;
    }
    return { app, bundle, load, connect, server, messages, get starts() { return starts; }, get socket() { return socket; } };
}

test("lifecycle repair works even when the IPC server cannot listen", async t => {
    const bridge = loadBridge(t, { failListen: true });
    await tick();
    assert.ok(bridge.messages.some(entry => String(entry).includes("listen denied")));
    assert.equal(bridge.app.exit(0), 0);
    assert.ok(fs.readFileSync(bridge.bundle, "utf8").includes(lifecycle.BEGIN));
});

test("missing runtime directory disables IPC but still installs autorepair", async t => {
    const bridge = loadBridge(t, { missingRuntime: true });
    await tick();
    assert.ok(bridge.messages.some(entry => String(entry).includes("XDG_RUNTIME_DIR is missing")));
    bridge.app.exit(0);
    assert.ok(fs.readFileSync(bridge.bundle, "utf8").includes(lifecycle.BEGIN));
});

test("duplicate bridge loading does not duplicate hooks or socket server", async t => {
    const bridge = loadBridge(t);
    await tick();
    bridge.load();
    assert.equal(bridge.starts, 1);
    assert.equal(bridge.app.listenerCount("before-quit"), 1);
    assert.equal(bridge.socket, true);
    bridge.app.exit(0);
    assert.equal(bridge.socket, false);
});

test("commands execute serially and acknowledgement follows renderer completion", async t => {
    const pending = [];
    const scripts = [];
    const window = {
        isDestroyed: () => false,
        webContents: {
            isDestroyed: () => false, isLoading: () => false,
            executeJavaScript(script) { scripts.push(script); return new Promise(resolve => pending.push(resolve)); }
        }
    };
    const bridge = loadBridge(t, { windows: [window] });
    await tick();
    const mute = bridge.connect("mute\n");
    const deafen = bridge.connect("deafen\n");
    await tick();
    assert.equal(scripts.length, 1);
    assert.equal(mute.response, undefined);
    assert.match(scripts[0], /Mute/);
    pending.shift()(true);
    await tick();
    assert.equal(mute.response, "OK\n");
    assert.equal(scripts.length, 2);
    assert.equal(deafen.response, undefined);
    assert.match(scripts[1], /Deafen/);
    pending.shift()(true);
    await tick();
    assert.equal(deafen.response, "OK\n");
});

test("protocol handles fragments, invalid commands and missing Discord controls", async t => {
    const bridge = loadBridge(t);
    await tick();
    const fragmented = bridge.connect("mu");
    assert.equal(fragmented.response, undefined);
    fragmented.emit("data", "te\n");
    assert.match(bridge.connect("unknown\n").response, /invalid command/);
    assert.match(bridge.connect("x".repeat(33)).response, /too long/);
    await tick();
    assert.match(fragmented.response, /button not found/);
    const next = bridge.connect("deafen\n");
    await tick();
    assert.match(next.response, /button not found/);
});
