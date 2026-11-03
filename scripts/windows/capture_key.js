// Adapted from freanocank/wechat-decrypt, MIT, f0b3f9f95dda2d954ee3dbe1d139fb1944a5ecfd.
// Only structured status/candidates cross this private pipe; never log native diagnostics.
'use strict';

function scanRanges(module, protection, pattern) {
  const hits = [];
  for (const range of module.enumerateRanges(protection)) {
    try {
      for (const hit of Memory.scanSync(range.base, range.size, pattern)) {
        hits.push({address: hit.address, range});
      }
    } catch (_) { /* Unreadable sections are not candidates. */ }
  }
  return hits;
}

function findEntry(lea, rangeBase) {
  for (let offset = 0; offset < 8192; offset++) {
    const address = lea.sub(offset);
    if (address.compare(rangeBase) <= 0) break;
    try {
      if (address.sub(1).readU8() === 0xcc) return address;
    } catch (_) { break; }
  }
  return null;
}

function candidate(pointer) {
  try {
    const bytes = new Uint8Array(pointer.readByteArray(128));
    for (let i = 32; i < 128; i++) if (bytes[i] !== 0x36) return null;
    let value = '';
    for (let i = 0; i < 32; i++) value += ((bytes[i] ^ 0x36) + 0x100).toString(16).slice(-2);
    return value;
  } catch (_) { return null; }
}

function installHooks(module) {
  if (Process.arch !== 'x64') throw new Error('architecture');
  const tables = new Set(scanRanges(module, 'r--', '22 ae 28 d7 98 2f 8a 42')
    .map(hit => hit.address.toString()));
  if (!tables.size) throw new Error('tables');
  const entries = new Map();
  for (const hit of scanRanges(module, 'r-x', '48 8d 05 : fb ff c7')) {
    try {
      if (hit.address.add(7).compare(hit.range.base.add(hit.range.size)) > 0) continue;
      const target = hit.address.add(7).add(hit.address.add(3).readS32());
      if (!tables.has(target.toString())) continue;
      const entry = findEntry(hit.address, hit.range.base);
      if (entry) entries.set(entry.toString(), entry);
    } catch (_) { /* Continue checking other SHA-512 implementations. */ }
  }
  const seen = new Set();
  let attached = 0;
  for (const entry of entries.values()) {
    try {
      Interceptor.attach(entry, {onEnter() {
        const value = candidate(this.context.rdx);
        if (value && !seen.has(value) && seen.size < 128) {
          seen.add(value);
          send({kind: 'candidate', value});
        }
      }});
      attached++;
    } catch (_) { /* This entry cannot be hooked. */ }
  }
  if (!attached) throw new Error('entries');
  send({kind: 'ready'});
}

let installed = false;
const observer = Process.attachModuleObserver({onAdded(module) {
  if (installed || module.name.toLowerCase() !== 'weixin.dll') return;
  installed = true;
  try { installHooks(module); }
  catch (_) { send({kind: 'error'}); }
}});
