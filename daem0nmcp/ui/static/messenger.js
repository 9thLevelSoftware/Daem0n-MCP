/* Hardened JSON-RPC bridge for MCP Apps views. */
class SecureMessenger {
    constructor(allowedOrigins = []) {
        this.allowedOrigins = new Set(["https://claude.ai", "https://desktop.claude.ai", "null", ...allowedOrigins]);
        // A compliant web host places the view inside its own sandbox proxy.
        // Pin that browser-supplied parent origin, rather than assuming the
        // proxy shares the product's public origin. Never trust a message to
        // tell us which origin to accept.
        this.parentOrigin = null;
        try {
            const referrer = new URL(document.referrer);
            const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(referrer.hostname);
            if (referrer.protocol === "https:" || (referrer.protocol === "http:" && loopback)) {
                this.parentOrigin = referrer.origin;
            }
        } catch (_error) { /* Native/opaque hosts may omit the referrer. */ }
        this.handlers = new Map();
        this.pendingRequests = new Map();
        this.nextId = 1;
        this.initialized = false;
        this.connected = false;
        this.connecting = null;
        this._listener = event => this._handleMessage(event);
    }

    init() {
        if (this.initialized) return;
        window.addEventListener("message", this._listener);
        this.initialized = true;
    }

    connect() {
        if (this.connected) return Promise.resolve(this.hostContext || {});
        if (this.connecting) return this.connecting;
        this.init();
        this.connecting = this.request("ui/initialize", {
            protocolVersion: "2026-01-26",
            appInfo: { name: "Daem0n MCP Apps", version: "7" },
            appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
        }).then(result => {
            if (!result || result.protocolVersion !== "2026-01-26") {
                throw new Error("Unsupported MCP Apps protocol version");
            }
            this.hostContext = result && typeof result === "object" ? result : {};
            this.connected = true;
            this.notify("ui/notifications/initialized", {});
            return this.hostContext;
        }).finally(() => { this.connecting = null; });
        return this.connecting;
    }

    _isBounded(value) {
        try { return new TextEncoder().encode(JSON.stringify(value)).byteLength <= 1_048_576; }
        catch (_error) { return false; }
    }

    _handleMessage(event) {
        // An opaque sandbox origin alone cannot identify the host.
        if (event.source !== window.parent) return;
        if (this.parentOrigin !== null ? event.origin !== this.parentOrigin : !this.allowedOrigins.has(event.origin)) return;
        const message = event.data;
        if (!message || typeof message !== "object" || Array.isArray(message) ||
            message.jsonrpc !== "2.0" || !this._isBounded(message)) return;
        if (message.id !== undefined && (message.result !== undefined || message.error !== undefined)) {
            this._handleResponse(message);
        } else if (typeof message.method === "string" && message.method.length <= 256) {
            this._handleRequest(message, event.source);
        }
    }

    _handleResponse(message) {
        const pending = this.pendingRequests.get(message.id);
        if (!pending) return;
        this.pendingRequests.delete(message.id);
        if (message.error) {
            const error = new Error(typeof message.error.message === "string" ? message.error.message : "Unknown error");
            error.code = message.error.code;
            pending.reject(error);
        } else pending.resolve(message.result);
    }

    _handleRequest(message, source) {
        const handler = this.handlers.get(message.method);
        if (!handler) {
            if (message.id !== undefined) this._sendResponse(source, message.id, null, { code: -32601, message: "Method not found" });
            return;
        }
        Promise.resolve().then(() => handler(message.params && typeof message.params === "object" ? message.params : {})).then(
            result => { if (message.id !== undefined) this._sendResponse(source, message.id, result); },
            _error => { if (message.id !== undefined) this._sendResponse(source, message.id, null, { code: -32603, message: "Internal error" }); },
        );
    }

    _sendResponse(target, id, result, error = null) {
        if (id === undefined) return;
        target.postMessage(error ? { jsonrpc: "2.0", id, error } : { jsonrpc: "2.0", id, result }, "*");
    }

    on(method, handler) {
        if (typeof handler !== "function") throw new Error("handler must be a function");
        this.handlers.set(method, handler);
    }
    off(method) { this.handlers.delete(method); }

    request(method, params = {}, timeout = 30_000) {
        if (!this.initialized) this.init();
        if (!this._isBounded(params)) return Promise.reject(new Error("Request payload exceeds limit"));
        return new Promise((resolve, reject) => {
            const id = this.nextId++;
            const timeoutId = setTimeout(() => {
                if (this.pendingRequests.delete(id)) reject(new Error("Request timeout: " + method));
            }, timeout);
            this.pendingRequests.set(id, {
                resolve: value => { clearTimeout(timeoutId); resolve(value); },
                reject: error => { clearTimeout(timeoutId); reject(error); },
            });
            window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
        });
    }

    send(method, params = {}) { this.notify(method, params); }
    notify(method, params = {}) {
        if (!this._isBounded(params)) return false;
        window.parent.postMessage({ jsonrpc: "2.0", method, params }, "*");
        return true;
    }
    addOrigin(origin) { if (typeof origin === "string") this.allowedOrigins.add(origin); }
    isOriginAllowed(origin) { return this.allowedOrigins.has(origin); }
    getMethods() { return Array.from(this.handlers.keys()); }
    destroy() {
        if (this.initialized) window.removeEventListener("message", this._listener);
        for (const pending of this.pendingRequests.values()) pending.reject(new Error("Messenger destroyed"));
        this.pendingRequests.clear();
        this.handlers.clear();
        this.initialized = false;
        this.connected = false;
    }
}

if (typeof module !== "undefined" && module.exports) module.exports = { SecureMessenger };
if (typeof window !== "undefined") {
    window.SecureMessenger = new SecureMessenger();
    window.SecureMessenger.init();
}
