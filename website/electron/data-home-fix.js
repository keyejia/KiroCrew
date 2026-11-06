"use strict";

// One-click repair for a data home another account owns (left by an earlier
// `sudo kirocrew ...`): give it back to the person running this app.
//
// The OS asks for the admin password itself -- macOS through `osascript ... with
// administrator privileges`, Linux through `pkexec` -- so no password ever
// reaches this process. The only command run with privilege is `chown -R -P
// <this uid> <data home>`: the target is the data home this process resolves
// itself (never a path from the renderer), it must be a real directory and not a
// link, and `-P` keeps the walk from following any link inside it. The
// owner-only modes are then restored as the person, with no privilege at all.
//
// Every binary is a fixed absolute path; nothing is looked up on PATH.

const nodeFs = require("fs");
const nodePath = require("path");
const childProcess = require("child_process");
const { resolveHome } = require("./home-dir");

const OSASCRIPT = "/usr/bin/osascript";
const PKEXEC_CANDIDATES = ["/usr/bin/pkexec", "/bin/pkexec"];
const CHOWN_CANDIDATES = {
  darwin: ["/usr/sbin/chown"],
  linux: ["/usr/bin/chown", "/bin/chown"],
};
const CHMOD_CANDIDATES = ["/bin/chmod", "/usr/bin/chmod"];

// osascript's "User canceled." (-128) and pkexec's dismissed dialog.
const OSASCRIPT_CANCELLED = "-128";
const PKEXEC_CANCELLED_CODES = new Set([126]);

function firstExisting(candidates, fs) {
  for (const candidate of candidates) {
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch { /* not here */ }
  }
  return null;
}

/** The elevation route this host offers, or null where none is available. */
function elevationRoute({ platform = process.platform, fs = nodeFs } = {}) {
  const chown = firstExisting(CHOWN_CANDIDATES[platform] || [], fs);
  if (!chown) return null;
  if (platform === "darwin") {
    return firstExisting([OSASCRIPT], fs) ? { kind: "osascript", bin: OSASCRIPT, chown } : null;
  }
  if (platform === "linux") {
    const pkexec = firstExisting(PKEXEC_CANDIDATES, fs);
    return pkexec ? { kind: "pkexec", bin: pkexec, chown } : null;
  }
  return null;
}

/**
 * The directory the repair may touch: the resolved data home, as a real path.
 * Throws when it is missing, a link, not a directory, or a filesystem root.
 */
function repairTarget(home, { fs = nodeFs, path = nodePath } = {}) {
  const st = fs.lstatSync(home);
  if (st.isSymbolicLink()) throw new Error("the data home is a link");
  if (!st.isDirectory()) throw new Error("the data home is not a directory");
  const real = fs.realpathSync(home);
  if (real === path.dirname(real)) throw new Error("the data home is a filesystem root");
  return real;
}

/** A POSIX shell word for *value*. */
function shellWord(value) {
  return `'${String(value).split("'").join("'\\''")}'`;
}

/** An AppleScript string literal body for *value*. */
function appleScriptString(value) {
  return String(value).split("\\").join("\\\\").split("\"").join("\\\"");
}

/** The [file, args] that run the privileged chown through *route*. */
function elevatedCommand(route, uid, target) {
  if (!Number.isInteger(uid) || uid <= 0) throw new Error("no ordinary account to give the files to");
  const chown = [route.chown, "-R", "-P", String(uid), target];
  if (route.kind === "osascript") {
    const line = chown.map(shellWord).join(" ");
    return [route.bin, ["-e", `do shell script "${appleScriptString(line)}" with administrator privileges`]];
  }
  return [route.bin, chown];
}

function run(execFile, file, args) {
  return new Promise((resolve) => {
    execFile(file, args, { timeout: 120_000 }, (error, _stdout, stderr) => {
      resolve({ error, stderr: String(stderr || "") });
    });
  });
}

/**
 * Ask the OS for admin rights and give the data home back to this account.
 * Resolves { ok: true } or { ok: false, reason: "unavailable" | "cancelled" | "failed" }.
 */
async function fixDataHome({
  platform = process.platform,
  fs = nodeFs,
  path = nodePath,
  execFile = childProcess.execFile,
  getuid = () => (typeof process.getuid === "function" ? process.getuid() : -1),
  home = resolveHome(),
  log = () => {},
} = {}) {
  const route = elevationRoute({ platform, fs });
  const uid = getuid();
  if (!route || !Number.isInteger(uid) || uid <= 0) return { ok: false, reason: "unavailable" };
  let target;
  try {
    target = repairTarget(home, { fs, path });
  } catch (error) {
    log(`data-home:fix refused: ${error.message}`);
    return { ok: false, reason: "failed" };
  }
  const [file, args] = elevatedCommand(route, uid, target);
  const elevated = await run(execFile, file, args);
  if (elevated.error) {
    const cancelled = route.kind === "osascript"
      ? elevated.stderr.includes(OSASCRIPT_CANCELLED)
      : PKEXEC_CANCELLED_CODES.has(elevated.error.code);
    log(`data-home:fix ${cancelled ? "cancelled" : "failed"}: ${elevated.error.message}`);
    return { ok: false, reason: cancelled ? "cancelled" : "failed" };
  }
  const chmod = firstExisting(CHMOD_CANDIDATES, fs);
  if (chmod) {
    const restored = await run(execFile, chmod, ["-R", "u+rwX", target]);
    if (restored.error) log(`data-home:fix chmod failed: ${restored.error.message}`);
  }
  return { ok: true };
}

module.exports = {
  elevationRoute,
  repairTarget,
  elevatedCommand,
  fixDataHome,
};
