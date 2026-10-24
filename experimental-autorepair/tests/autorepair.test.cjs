const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { BEGIN, END, stripPatch, patchContent, repair, installHooks } = require("../autorepair.js");
const injectionFile = path.resolve(__dirname, "../injection.js");
const injection = fs.readFileSync(injectionFile, "utf8");

function fixture(t, clean = "globalThis.version = 2;\n//# sourceURL=VencordDesktopMain") {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vcc-autorepair-"));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const bundle = path.join(dir, "vencordDesktopMain.js");
    fs.writeFileSync(bundle, clean, { mode: 0o640 });
    return { dir, bundle, clean, injectionFile };
}

test("insertion and removal preserve all original bytes including line endings", () => {
    for (const clean of ["", "a();", "a();\n", "a();\r\n", "// коммент", "// sourceURL=x\r\n\r\n"]) {
        const patched = patchContent(clean, injection);
        assert.equal(stripPatch(patched), clean);
        assert.equal(patchContent(patched, injection), patched);
    }
});

test("validation does not execute bundle code", () => {
    const code = 'throw new Error("must not execute");';
    assert.equal(stripPatch(patchContent(code, injection)), code);
});

test("broken markers, old VCC and invalid JavaScript are rejected", () => {
    for (const content of [BEGIN, END, injection + injection, `${BEGIN}\n${END}`, "/* vesktopctl begin */", "/* VesktopCustomCommands Injection */", "const broken = ;"]) {
        assert.throws(() => patchContent(content, injection));
    }
    assert.throws(() => patchContent("", "not an injection"));
});

test("repair is atomic, preserves permissions, and does not rewrite an existing patch", t => {
    const f = fixture(t);
    assert.equal(repair(f.bundle, injectionFile), true);
    const before = fs.statSync(f.bundle);
    assert.equal(before.mode & 0o777, 0o640);
    assert.equal(stripPatch(fs.readFileSync(f.bundle, "utf8")), f.clean);
    assert.equal(repair(f.bundle, injectionFile), false);
    assert.equal(fs.statSync(f.bundle).ino, before.ino);
    assert.equal(fs.statSync(f.bundle).mtimeMs, before.mtimeMs);
    assert.deepEqual(fs.readdirSync(f.dir), ["vencordDesktopMain.js"]);
});

test("failed replacement leaves the original file and cleans its temporary file", t => {
    const f = fixture(t);
    const rename = fs.renameSync;
    fs.renameSync = () => { throw Object.assign(new Error("read-only"), { code: "EACCES" }); };
    try { assert.throws(() => repair(f.bundle, injectionFile), /read-only/); }
    finally { fs.renameSync = rename; }
    assert.equal(fs.readFileSync(f.bundle, "utf8"), f.clean);
    assert.deepEqual(fs.readdirSync(f.dir), ["vencordDesktopMain.js"]);
});

test("a concurrent writer is detected before replacement", t => {
    const f = fixture(t);
    const lstat = fs.lstatSync;
    let reads = 0;
    fs.lstatSync = function (filename, ...args) {
        if (filename === f.bundle && ++reads === 2) fs.writeFileSync(f.bundle, "another writer();");
        return lstat.call(this, filename, ...args);
    };
    try { assert.throws(() => repair(f.bundle, injectionFile), /changed during repair/); }
    finally { fs.lstatSync = lstat; }
    assert.equal(fs.readFileSync(f.bundle, "utf8"), "another writer();");
    assert.deepEqual(fs.readdirSync(f.dir), ["vencordDesktopMain.js"]);
});

test("missing files, invalid UTF-8, invalid syntax and symlinks are not overwritten", t => {
    const f = fixture(t);
    assert.throws(() => repair(path.join(f.dir, "missing.js"), injectionFile));
    for (const data of [Buffer.from([0xff]), Buffer.from("const broken = ;")]) {
        fs.writeFileSync(f.bundle, data);
        assert.throws(() => repair(f.bundle, injectionFile));
        assert.deepEqual(fs.readFileSync(f.bundle), data);
    }
    const link = path.join(f.dir, "link.js");
    fs.symlinkSync(f.bundle, link);
    assert.throws(() => repair(link, injectionFile), /regular file/);
});

test("relaunch and exit repair before originals, preserve this/args/results and register once", t => {
    const f = fixture(t);
    const app = new EventEmitter();
    const calls = [];
    const logger = { info() {}, error(error) { throw error; } };
    for (const name of ["relaunch", "exit"]) {
        app[name] = function (...args) {
            assert.equal(this, app);
            assert.equal(fs.readFileSync(f.bundle, "utf8"), f.clean + injection);
            calls.push([name, ...args]);
            return name;
        };
    }
    let cleanups = 0;
    const options = { ...f, logger, cleanup() { cleanups++; } };
    installHooks(app, options);
    const exit = app.exit;
    installHooks(app, options);
    assert.equal(app.exit, exit);
    assert.equal(app.listenerCount("before-quit"), 1);
    const args = { args: ["--relaunch"] };
    assert.equal(app.relaunch(args), "relaunch");
    assert.equal(cleanups, 0);
    assert.equal(app.exit(7), "exit");
    assert.equal(cleanups, 1);
    assert.deepEqual(calls, [["relaunch", args], ["exit", 7]]);
    fs.writeFileSync(f.bundle, "globalThis.version = 3;\r\n");
    app.emit("before-quit");
    assert.equal(stripPatch(fs.readFileSync(f.bundle, "utf8")), "globalThis.version = 3;\r\n");
    assert.equal(cleanups, 2);
});

test("repair and cleanup errors do not suppress the original Electron call", t => {
    const f = fixture(t, "const broken = ;");
    const app = new EventEmitter();
    const errors = [];
    const calls = [];
    app.exit = (...args) => { calls.push(args); return 17; };
    app.relaunch = () => 23;
    installHooks(app, { ...f, cleanup() { throw new Error("socket error"); }, logger: { info() {}, error(...args) { errors.push(args); } } });
    assert.equal(app.exit(4), 17);
    assert.deepEqual(calls, [[4]]);
    assert.equal(errors.length, 2);
    assert.equal(app.relaunch(), 23);
    assert.equal(fs.readFileSync(f.bundle, "utf8"), f.clean);
});

test("readonly Electron methods roll back partially installed hooks", t => {
    const f = fixture(t);
    const app = new EventEmitter();
    const relaunch = () => {};
    app.relaunch = relaunch;
    Object.defineProperty(app, "exit", { value() {}, writable: false });
    assert.throws(() => installHooks(app, f));
    assert.equal(app.relaunch, relaunch);
    assert.equal(app.listenerCount("before-quit"), 0);
});
