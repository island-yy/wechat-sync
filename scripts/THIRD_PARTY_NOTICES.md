# References and third-party notices

The bundled native/libwechat_reader.dylib contains SQLCipher 4.19.0
(c4b275a47932888216bade83aff2bbc73df0ff85), including its SQLite core,
and the Zstandard 1.5.7 decoder (f8745da6ff1ad1e7bab384bd1f9d742439278e99).
Both libraries are redistributed under their BSD licenses, reproduced in
native/SQLCipher-LICENSE.txt and native/Zstandard-LICENSE.txt. SQLite itself
is public domain. Exact source archives, build parameters and SHA256 digests
are recorded in native/BUILD.json. CommonCrypto, Security.framework and
CoreFoundation.framework are provided by macOS and are not redistributed.

Windows installs pinned upstream wheels (Frida 17.2.17, sqlcipher3 0.6.4,
Zstandard 0.25.0) into a private virtual environment; these wheels are not
redistributed in this skill. sqlcipher3's set_key API passes bytes directly
to sqlite3_key. Sources: https://github.com/coleifer/sqlcipher3,
https://github.com/frida/frida-python, https://github.com/indygreg/python-zstandard.

windows/capture_key.js is adapted from scripts/windows/extract_raw_key.py in
https://github.com/freanocank/wechat-decrypt, revision
f0b3f9f95dda2d954ee3dbe1d139fb1944a5ecfd. It retains dynamic SHA-512 entry
discovery, with bounded structured candidate transport and full-page HMAC
validation in the Python controller. Its upstream license follows:

MIT License

Copyright (c) 2026 cocofeng

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The passphrase capture entry point and SQLCipher parameter conventions were
informed by TANGandXUE/wcdb-key-tool, revision
79f1b5b92e12c66aa281b4a60a3c478b5f547dfa.

Source: https://github.com/TANGandXUE/wcdb-key-tool

Upstream license follows:

MIT License

Copyright (c) 2025 CloudDreamAI / TANGandXUE

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
