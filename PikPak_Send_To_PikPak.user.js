// ==UserScript==
// @name               PikPak Send To PikPak
// @name:ko            PikPak Send To PikPak
// @namespace          https://github.com/digbug82/
// @version            0.1.0
// @author             digbug82
// @license            CC-BY-NC-SA-4.0
// @description        Send magnet and torrent links from webpages to PikPak Enhancement Master.
// @description:ko     웹페이지의 마그넷 및 토렌트 링크를 PikPak Enhancement Master로 전달합니다.
// @match              *://*/*
// @exclude            https://mypikpak.com/drive/*
// @exclude            https://app.mypikpak.com/*
// @exclude            https://drive.mypikpak.com/*
// @compatible         chrome
// @compatible         edge
// @grant              GM_registerMenuCommand
// @grant              GM_xmlhttpRequest
// @grant              GM_openInTab
// @grant              GM_notification
// @connect            *
// @run-at             document-idle
// ==/UserScript==

(() => {
"use strict";

if (/(^|\.)mypikpak\.com$/i.test(location.hostname)) return;

const PIKPAK_TARGET_URL = "https://drive.mypikpak.com/";
const MAX_TORRENT_BYTES = 12 * 1024 * 1024;

let lastContext = {
    href: "",
    text: "",
    title: ""
};

const notify = (message, title = "PikPak") => {
    if (typeof GM_notification === "function") {
        try {
            GM_notification({ title, text: message, timeout: 4500 });
            return;
        } catch (e) {}
    }
    console.log(`[${title}] ${message}`);
};

const cleanUrlText = (value) => String(value || "")
    .trim()
    .replace(/&amp;/gi, "&")
    .replace(/[\])}>.,;]+$/g, "");

const closestAnchor = (target) => {
    if (!target || typeof target.closest !== "function") return null;
    return target.closest("a[href]");
};

const getSelectionText = (doc = document) => {
    try {
        const win = doc && doc.defaultView ? doc.defaultView : window;
        return String(win.getSelection ? win.getSelection() : "").trim();
    } catch (e) {
        return "";
    }
};

const base32ToHex = (value) => {
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let bits = "";
    const input = String(value || "").toUpperCase();
    for (let i = 0; i < input.length; i++) {
        const idx = alphabet.indexOf(input[i]);
        if (idx < 0) return "";
        bits += idx.toString(2).padStart(5, "0");
    }
    let hex = "";
    for (let i = 0; i + 4 <= bits.length; i += 4) {
        hex += parseInt(bits.slice(i, i + 4), 2).toString(16);
    }
    return hex.toUpperCase();
};

const normalizeMagnet = (value) => {
    let link = cleanUrlText(value);
    if (!link) return "";

    const urnOnly = link.match(/^urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})(?:[^\s"'<>`]*)?$/i);
    if (urnOnly) link = `magnet:?xt=urn:btih:${urnOnly[1].toUpperCase()}`;

    const pureHash = link.match(/^([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})$/i);
    if (pureHash) link = `magnet:?xt=urn:btih:${pureHash[1].toUpperCase()}`;

    if (!/^magnet:\?/i.test(link)) return "";

    const hashMatch = link.match(/[?&]xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})/i);
    if (!hashMatch) return "";

    const hash = hashMatch[1];
    if (hash.length === 32) {
        const hex = base32ToHex(hash);
        if (!hex) return "";
        link = link.replace(hash, hex);
    }

    return link;
};

const extractMagnets = (text) => {
    const input = String(text || "");
    const unique = new Map();
    const add = (value) => {
        const magnet = normalizeMagnet(value);
        if (!magnet) return;
        const hashMatch = magnet.match(/[?&]xt=urn:btih:([^&]+)/i);
        const key = hashMatch ? hashMatch[1].toUpperCase() : magnet;
        if (!unique.has(key)) unique.set(key, magnet);
    };

    let match;
    const magnetRegex = /magnet:\?[^\s"'<>`]+/gi;
    while ((match = magnetRegex.exec(input))) add(match[0]);

    const urnRegex = /urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})(?:[^\s"'<>`]*)?/gi;
    while ((match = urnRegex.exec(input))) add(match[0]);

    const trimmed = input.trim();
    if (unique.size === 0 && /^([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})$/i.test(trimmed)) add(trimmed);

    return Array.from(unique.values());
};

const asAbsoluteHttpUrl = (value) => {
    const text = cleanUrlText(value);
    if (!text) return "";
    try {
        const url = new URL(text, location.href);
        return /^https?:$/i.test(url.protocol) ? url.href : "";
    } catch (e) {
        return "";
    }
};

const isLikelyTorrentUrl = (href, label = "") => {
    const url = asAbsoluteHttpUrl(href);
    if (!url) return false;
    try {
        const parsed = new URL(url);
        if (/\.torrent$/i.test(parsed.pathname)) return true;
    } catch (e) {}

    const haystack = `${href} ${label}`.toLowerCase();
    return /\btorrent\b/.test(haystack) && /(download|torrent|bt|file|attachment|magnet)/.test(haystack);
};

const extractTorrentUrls = (text) => {
    const unique = new Map();
    const urlRegex = /https?:\/\/[^\s"'<>`]+/gi;
    let match;
    while ((match = urlRegex.exec(String(text || "")))) {
        const url = asAbsoluteHttpUrl(match[0]);
        if (url && isLikelyTorrentUrl(url)) unique.set(url, url);
    }
    return Array.from(unique.values());
};

const encodePayload = (payload) => {
    const json = JSON.stringify(payload);
    const bytes = new TextEncoder().encode(json);
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode(...bytes.slice(i, i + 0x8000));
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
};

const openPikPak = (items) => {
    const payload = {
        v: 1,
        autoClose: true,
        createdAt: Date.now(),
        source: {
            url: location.href,
            title: document.title || ""
        },
        items
    };
    const token = encodePayload(payload);
    const url = `${PIKPAK_TARGET_URL}#pk-add=${encodeURIComponent(token)}`;

    if (typeof GM_openInTab === "function") {
        try {
            GM_openInTab(url, { active: false, insert: true, setParent: true });
            return;
        } catch (e) {}
    }
    window.open(url, "_blank");
};

const sendMagnetsToPikPak = (magnets) => {
    const unique = new Map();
    magnets.forEach(link => {
        const magnet = normalizeMagnet(link);
        if (!magnet) return;
        const hashMatch = magnet.match(/[?&]xt=urn:btih:([^&]+)/i);
        const key = hashMatch ? hashMatch[1].toUpperCase() : magnet;
        if (!unique.has(key)) unique.set(key, { type: "magnet", url: magnet });
    });

    const items = Array.from(unique.values());
    if (items.length === 0) {
        notify("유효한 마그넷/토렌트 링크를 찾지 못했습니다.");
        return;
    }

    openPikPak(items);
    notify(`PikPak에 추가 요청 완료: ${items.length}개`);
};

const gmArrayBufferRequest = (url) => new Promise((resolve, reject) => {
    if (typeof GM_xmlhttpRequest !== "function") {
        reject(new Error("GM_xmlhttpRequest is not available."));
        return;
    }

    GM_xmlhttpRequest({
        method: "GET",
        url,
        responseType: "arraybuffer",
        timeout: 30000,
        headers: {
            Accept: "application/x-bittorrent, application/octet-stream, */*"
        },
        onload: (res) => {
            if (res.status && (res.status < 200 || res.status >= 300)) {
                reject(new Error(`HTTP ${res.status}`));
                return;
            }
            const buffer = res.response;
            if (!(buffer instanceof ArrayBuffer)) {
                reject(new Error("Invalid torrent response."));
                return;
            }
            if (buffer.byteLength > MAX_TORRENT_BYTES) {
                reject(new Error("Torrent file is too large."));
                return;
            }
            resolve(buffer);
        },
        onerror: () => reject(new Error("Network error.")),
        ontimeout: () => reject(new Error("Request timed out."))
    });
});

const rotl = (value, bits) => ((value << bits) | (value >>> (32 - bits))) >>> 0;

const sha1HexSync = (bytes) => {
    const messageLength = bytes.length;
    const paddedLength = (((messageLength + 1 + 8 + 63) >> 6) << 6);
    const words = new Uint32Array(paddedLength / 4);

    for (let i = 0; i < messageLength; i++) {
        words[i >> 2] |= bytes[i] << (24 - ((i & 3) * 8));
    }

    words[messageLength >> 2] |= 0x80 << (24 - ((messageLength & 3) * 8));

    const bitLength = messageLength * 8;
    words[words.length - 2] = Math.floor(bitLength / 0x100000000);
    words[words.length - 1] = bitLength >>> 0;

    let h0 = 0x67452301;
    let h1 = 0xefcdab89;
    let h2 = 0x98badcfe;
    let h3 = 0x10325476;
    let h4 = 0xc3d2e1f0;
    const w = new Uint32Array(80);

    for (let i = 0; i < words.length; i += 16) {
        for (let t = 0; t < 16; t++) w[t] = words[i + t];
        for (let t = 16; t < 80; t++) w[t] = rotl(w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16], 1);

        let a = h0;
        let b = h1;
        let c = h2;
        let d = h3;
        let e = h4;

        for (let t = 0; t < 80; t++) {
            let f;
            let k;
            if (t < 20) {
                f = (b & c) | (~b & d);
                k = 0x5a827999;
            } else if (t < 40) {
                f = b ^ c ^ d;
                k = 0x6ed9eba1;
            } else if (t < 60) {
                f = (b & c) | (b & d) | (c & d);
                k = 0x8f1bbcdc;
            } else {
                f = b ^ c ^ d;
                k = 0xca62c1d6;
            }

            const temp = (rotl(a, 5) + f + e + k + w[t]) >>> 0;
            e = d;
            d = c;
            c = rotl(b, 30);
            b = a;
            a = temp;
        }

        h0 = (h0 + a) >>> 0;
        h1 = (h1 + b) >>> 0;
        h2 = (h2 + c) >>> 0;
        h3 = (h3 + d) >>> 0;
        h4 = (h4 + e) >>> 0;
    }

    const hex = (value) => (value >>> 0).toString(16).padStart(8, "0");
    return `${hex(h0)}${hex(h1)}${hex(h2)}${hex(h3)}${hex(h4)}`;
};

const sha1Hex = async (bytes) => {
    if (globalThis.crypto && crypto.subtle && typeof crypto.subtle.digest === "function") {
        try {
            const hash = await crypto.subtle.digest("SHA-1", bytes);
            return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, "0")).join("");
        } catch (e) {}
    }
    return sha1HexSync(bytes);
};

const torrentBufferToMagnet = async (buffer, name = "") => {
    const buf = new Uint8Array(buffer);
    if (buf[0] !== 100) throw new Error("Invalid torrent file.");

    let pos = 0;
    let iterations = 0;
    const maxIterations = 120000;

    const failIfUnsafe = () => {
        if (++iterations > maxIterations) throw new Error("Torrent file is too complex.");
        if (pos > buf.length) throw new Error("Invalid torrent structure.");
    };

    const readNumber = (start, end) => {
        let value = 0;
        if (start >= end) throw new Error("Invalid torrent length.");
        for (let i = start; i < end; i++) {
            const code = buf[i];
            if (code < 48 || code > 57) throw new Error("Invalid torrent length.");
            value = value * 10 + (code - 48);
        }
        return value;
    };

    const readAscii = (start, end) => {
        let out = "";
        for (let i = start; i < end; i++) out += String.fromCharCode(buf[i]);
        return out;
    };

    const readString = () => {
        failIfUnsafe();
        const lenStart = pos;
        while (pos < buf.length && buf[pos] !== 58) pos++;
        if (pos >= buf.length) throw new Error("Invalid torrent string.");
        const len = readNumber(lenStart, pos);
        const start = pos + 1;
        const end = start + len;
        if (end > buf.length) throw new Error("Invalid torrent string length.");
        pos = end;
        return { start, end, text: readAscii(start, end) };
    };

    const skipValue = () => {
        failIfUnsafe();
        const code = buf[pos];
        if (code === 100 || code === 108) {
            pos++;
            while (pos < buf.length && buf[pos] !== 101) skipValue();
            if (buf[pos] !== 101) throw new Error("Invalid torrent container.");
            pos++;
            return;
        }
        if (code === 105) {
            pos++;
            while (pos < buf.length && buf[pos] !== 101) pos++;
            if (buf[pos] !== 101) throw new Error("Invalid torrent integer.");
            pos++;
            return;
        }
        if (code >= 48 && code <= 57) {
            readString();
            return;
        }
        throw new Error("Invalid torrent value.");
    };

    pos = 1;
    while (pos < buf.length && buf[pos] !== 101) {
        const key = readString().text;
        if (key === "info") {
            const infoStart = pos;
            skipValue();
            const infoEnd = pos;
            const infoHash = await sha1Hex(buf.slice(infoStart, infoEnd));
            const dn = name ? `&dn=${encodeURIComponent(name.replace(/\.torrent$/i, ""))}` : "";
            return `magnet:?xt=urn:btih:${infoHash}${dn}`;
        }
        skipValue();
    }

    throw new Error("Torrent info section was not found.");
};

const filenameFromUrl = (url) => {
    try {
        const name = decodeURIComponent(new URL(url).pathname.split("/").filter(Boolean).pop() || "");
        return name || "download.torrent";
    } catch (e) {
        return "download.torrent";
    }
};

const torrentUrlToMagnet = async (url, label = "") => {
    notify("토렌트 파일을 확인하는 중...");
    const buffer = await gmArrayBufferRequest(url);
    return torrentBufferToMagnet(buffer, label || filenameFromUrl(url));
};

const buildItemsFromContext = async (context) => {
    const texts = [
        context.href,
        context.text,
        context.title,
        context.selection,
        getSelectionText()
    ].filter(Boolean);

    const magnets = [];
    texts.forEach(text => magnets.push(...extractMagnets(text)));

    const torrentUrls = new Map();
    if (context.href && isLikelyTorrentUrl(context.href, `${context.text} ${context.title}`)) {
        const url = asAbsoluteHttpUrl(context.href);
        if (url) torrentUrls.set(url, { url, label: context.text || context.title || filenameFromUrl(url) });
    }
    texts.forEach(text => {
        extractTorrentUrls(text).forEach(url => {
            if (!torrentUrls.has(url)) torrentUrls.set(url, { url, label: filenameFromUrl(url) });
        });
    });

    for (const item of torrentUrls.values()) {
        try {
            magnets.push(await torrentUrlToMagnet(item.url, item.label));
        } catch (e) {
            notify(`토렌트 링크를 읽지 못했습니다: ${e.message || e}`);
        }
    }

    return magnets;
};

const sendContextToPikPak = async (context = lastContext) => {
    try {
        const magnets = await buildItemsFromContext(context);
        sendMagnetsToPikPak(magnets);
    } catch (e) {
        notify(e.message || String(e));
    }
};

const boundDocuments = new WeakSet();
const boundFrames = new WeakSet();

const bindDocument = (doc) => {
    if (!doc || boundDocuments.has(doc)) return;
    boundDocuments.add(doc);

    doc.addEventListener("contextmenu", (event) => {
        const anchor = closestAnchor(event.target);
        const selection = getSelectionText(doc);
        lastContext = {
            href: anchor ? anchor.href : "",
            text: selection || (anchor ? anchor.textContent || "" : ""),
            title: anchor ? anchor.getAttribute("title") || "" : "",
            selection
        };
    }, true);

    doc.addEventListener("click", (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
        const anchor = closestAnchor(event.target);
        if (!anchor) return;

        const href = anchor.href || anchor.getAttribute("href") || "";
        const label = anchor.textContent || anchor.getAttribute("title") || "";
        const isDirectMagnet = !!normalizeMagnet(href);
        const isTorrent = isLikelyTorrentUrl(href, label);
        if (!isDirectMagnet && !isTorrent) return;

        event.preventDefault();
        event.stopPropagation();

        sendContextToPikPak({
            href,
            text: label,
            title: anchor.getAttribute("title") || "",
            selection: getSelectionText(doc)
        });
    }, true);
};

const bindSameOriginFrame = (frame) => {
    if (!frame) return;
    if (!boundFrames.has(frame)) {
        boundFrames.add(frame);
        frame.addEventListener("load", () => bindSameOriginFrame(frame));
    }
    try {
        if (frame.contentDocument) bindDocument(frame.contentDocument);
    } catch (e) {}
};

const bindSameOriginFrames = () => {
    try {
        document.querySelectorAll("iframe").forEach(bindSameOriginFrame);
    } catch (e) {}
};

bindDocument(document);
bindSameOriginFrames();

const observer = new MutationObserver((records) => {
    for (const record of records) {
        record.addedNodes.forEach(node => {
            if (!node || node.nodeType !== 1) return;
            if (node.tagName === "IFRAME") bindSameOriginFrame(node);
            if (node.querySelectorAll) node.querySelectorAll("iframe").forEach(bindSameOriginFrame);
        });
    }
});
observer.observe(document.documentElement || document.body, { childList: true, subtree: true });

window.addEventListener("load", bindSameOriginFrames);
window.addEventListener("pageshow", bindSameOriginFrames);

if (typeof GM_registerMenuCommand === "function") {
    GM_registerMenuCommand("PikPak으로 magnet/torrent 보내기", () => {
        sendContextToPikPak(lastContext);
    });
}

window.addEventListener("message", (event) => {
    const data = event && event.data;
    if (!data || data.type !== "pk-external-add-result") return;

    const success = Number(data.successCount || 0);
    const fail = Number(data.failCount || 0);
    const skipped = Number(data.skippedCount || 0);
    if (fail > 0 || skipped > 0) {
        notify(`PikPak 추가 완료: ${success}개, 건너뜀: ${skipped}개, 실패: ${fail}개`);
    } else {
        notify(`PikPak 추가 완료: ${success}개`);
    }
});
})();
