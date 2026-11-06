"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const {
  elevationRoute,
  repairTarget,
  elevatedCommand,
  fixDataHome,
} = require(path.join(__dirname, "..", "data-home-fix.js"));

const HOME = "/srv/crew-data";

/** In-memory fs: `entries` maps an absolute path to "file", "dir" or {link: target}. */
function fakeFs(entries) {
  const kind = (p) => entries[p];
  const stat = (p, follow) => {
    let k = kind(p);
    if (follow && k && typeof k === "object") k = kind(k.link);
    if (!k) { const e = new Error(`ENOENT ${p}`); e.code = "ENOENT"; throw e; }
    return {
      isFile: () => k === "file",
      isDirectory: () => k === "dir",
      isSymbolicLink: () => typeof k === "object",
    };
  };
  return {
    statSync: (p) => stat(p, true),
    lstatSync: (p) => stat(p, false),
    realpathSync: (p) => (typeof kind(p) === "object" ? kind(p).link : p),
  };
}

const MAC = { "/usr/bin/osascript": "file", "/usr/sbin/chown": "file", "/bin/chmod": "file", [HOME]: "dir" };
const LINUX = { "/usr/bin/pkexec": "file", "/usr/bin/chown": "file", "/bin/chmod": "file", [HOME]: "dir" };

/** A fake execFile that records each call and answers from `answers` in order. */
function fakeExec(answers = []) {
  const calls = [];
  const execFile = (file, args, _opts, cb) => {
    calls.push([file, args]);
    const answer = answers.shift() || {};
    cb(answer.error || null, "", answer.stderr || "");
  };
  return { calls, execFile };
}

test("macOS and Linux offer a route only where their prompt and chown exist", () => {
  assert.equal(elevationRoute({ platform: "darwin", fs: fakeFs(MAC) }).kind, "osascript");
  assert.equal(elevationRoute({ platform: "linux", fs: fakeFs(LINUX) }).kind, "pkexec");
  assert.equal(elevationRoute({ platform: "linux", fs: fakeFs({ "/usr/bin/chown": "file" }) }), null);
  assert.equal(elevationRoute({ platform: "win32", fs: fakeFs(MAC) }), null);
});

test("the target is the data home's real directory, never a link or a root", () => {
  assert.equal(repairTarget(HOME, { fs: fakeFs({ [HOME]: "dir" }) }), HOME);
  assert.throws(() => repairTarget(HOME, { fs: fakeFs({ [HOME]: { link: "/" }, "/": "dir" }) }), /link/);
  assert.throws(() => repairTarget(HOME, { fs: fakeFs({ [HOME]: "file" }) }), /not a directory/);
  assert.throws(() => repairTarget(HOME, { fs: fakeFs({}) }), /ENOENT/);
  assert.throws(() => repairTarget("/", { fs: fakeFs({ "/": "dir" }) }), /root/);
});

test("pkexec runs only chown, recursive without following links, to this uid", () => {
  const [file, args] = elevatedCommand({ kind: "pkexec", bin: "/usr/bin/pkexec", chown: "/usr/bin/chown" }, 501, HOME);
  assert.equal(file, "/usr/bin/pkexec");
  assert.deepEqual(args, ["/usr/bin/chown", "-R", "-P", "501", HOME]);
});

test("osascript quotes the path for both the shell and AppleScript", () => {
  const odd = `/srv/it's "crew"`;
  const [file, args] = elevatedCommand({ kind: "osascript", bin: "/usr/bin/osascript", chown: "/usr/sbin/chown" }, 501, odd);
  assert.equal(file, "/usr/bin/osascript");
  assert.equal(args[0], "-e");
  assert.equal(
    args[1],
    `do shell script "'/usr/sbin/chown' '-R' '-P' '501' '/srv/it'\\\\''s \\"crew\\"'" with administrator privileges`,
  );
});

test("root or an unknown uid is never the account the files go to", () => {
  const route = { kind: "pkexec", bin: "/usr/bin/pkexec", chown: "/usr/bin/chown" };
  assert.throws(() => elevatedCommand(route, 0, HOME));
  assert.throws(() => elevatedCommand(route, -1, HOME));
});

test("a successful fix chowns with privilege, then restores modes as the person", async () => {
  const exec = fakeExec();
  const result = await fixDataHome({ platform: "linux", fs: fakeFs(LINUX), execFile: exec.execFile, getuid: () => 1000, home: HOME });
  assert.deepEqual(result, { ok: true });
  assert.deepEqual(exec.calls, [
    ["/usr/bin/pkexec", ["/usr/bin/chown", "-R", "-P", "1000", HOME]],
    ["/bin/chmod", ["-R", "u+rwX", HOME]],
  ]);
});

test("a dismissed admin prompt is reported as cancelled and changes nothing else", async () => {
  const mac = fakeExec([{ error: Object.assign(new Error("exit 1"), { code: 1 }), stderr: "execution error: User canceled. (-128)" }]);
  assert.deepEqual(
    await fixDataHome({ platform: "darwin", fs: fakeFs(MAC), execFile: mac.execFile, getuid: () => 501, home: HOME }),
    { ok: false, reason: "cancelled" },
  );
  assert.equal(mac.calls.length, 1);
  const linux = fakeExec([{ error: Object.assign(new Error("exit 126"), { code: 126 }) }]);
  assert.deepEqual(
    await fixDataHome({ platform: "linux", fs: fakeFs(LINUX), execFile: linux.execFile, getuid: () => 1000, home: HOME }),
    { ok: false, reason: "cancelled" },
  );
});

test("nothing runs without a route, as root, or on a linked data home", async () => {
  for (const [label, opts] of [
    ["no route", { platform: "win32", fs: fakeFs(MAC), getuid: () => 501 }],
    ["root", { platform: "darwin", fs: fakeFs(MAC), getuid: () => 0 }],
    ["link", { platform: "darwin", fs: fakeFs({ ...MAC, [HOME]: { link: "/etc" }, "/etc": "dir" }), getuid: () => 501 }],
  ]) {
    const exec = fakeExec();
    const result = await fixDataHome({ ...opts, execFile: exec.execFile, home: HOME });
    assert.equal(result.ok, false, label);
    assert.equal(exec.calls.length, 0, `${label}: nothing may run`);
  }
});
