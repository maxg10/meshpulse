// MeshPulse admin password: shared by the map, Stats, Messages and Config.
// Copyright (C) 2026 Mariusz "Max" Gieparda · GPL-3.0
//
// When an admin password is set (Config → Security), the backend refuses the
// WebSocket requests that change the system or transmit until the connection
// has logged in. This file sits between each page and its WebSocket: it logs in
// with a remembered token, holds back protected requests while it asks for the
// password, and sends them once the password is accepted. Pages only call
// MeshAuth.attach(ws) after creating their socket.
//
// Without a password set, nothing here does anything.

(function () {
    'use strict';

    var KEY = 'meshpulse-admin-token';
    var S = {
        known: false,       // has the backend told us whether a password is set?
        required: false,
        authed: false,
        protected: {},
        ws: null,
        raw: null,
        queue: [],          // protected requests waiting for the password
        last: {},           // last request of each protected type, to resend after a refusal
        tokenPending: false,
        listeners: []
    };

    function getToken() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
    function setToken(t) {
        try { if (t) localStorage.setItem(KEY, t); else localStorage.removeItem(KEY); } catch (e) { /* private mode */ }
    }
    function typeOf(msg) {
        if (typeof msg !== 'string') return null;
        try { var d = JSON.parse(msg); return d && d.type; } catch (e) { return null; }
    }
    function rawSend(m) { if (S.raw && S.ws && S.ws.readyState === 1) S.raw(m); }
    function flush() { var q = S.queue; S.queue = []; q.forEach(rawSend); }
    function notify() {
        var st = state();
        S.listeners.forEach(function (cb) { try { cb(st); } catch (e) { /* a listener's problem */ } });
    }
    function state() { return {known: S.known, required: S.required, authed: S.authed}; }

    // ── the password dialog ─────────────────────────────────────────────

    var dlg = null;

    function css() {
        if (document.getElementById('mp-auth-css')) return;
        var st = document.createElement('style');
        st.id = 'mp-auth-css';
        st.textContent =
            '.mp-auth-bg{position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px}' +
            '.mp-auth{width:100%;max-width:340px;background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:10px;padding:18px 18px 14px;' +
            'font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;box-shadow:0 10px 40px rgba(0,0,0,.5)}' +
            '.mp-auth h3{margin:0 0 6px;font-size:16px}' +
            '.mp-auth p{margin:0 0 12px;color:#9ca3af;font-size:13px}' +
            '.mp-auth input{width:100%;box-sizing:border-box;padding:9px 10px;border-radius:6px;border:1px solid #4b5563;background:#1f2937;color:#f9fafb;font-size:15px}' +
            '.mp-auth .mp-err{min-height:18px;margin:6px 0 4px;color:#fca5a5;font-size:12px}' +
            '.mp-auth .mp-row{display:flex;gap:8px;justify-content:flex-end}' +
            '.mp-auth button{padding:8px 14px;border-radius:6px;border:1px solid #4b5563;background:transparent;color:#e5e7eb;font-size:13px;cursor:pointer}' +
            '.mp-auth button.mp-go{background:#2563eb;border-color:#2563eb;color:#fff}' +
            '.mp-auth button:disabled{opacity:.6;cursor:default}';
        document.head.appendChild(st);
    }

    function prompt() {
        if (dlg || S.tokenPending) return;
        css();
        dlg = document.createElement('div');
        dlg.className = 'mp-auth-bg';
        dlg.innerHTML =
            '<form class="mp-auth" autocomplete="on">' +
            '<h3>🔒 Admin password</h3>' +
            '<p>Changing settings and plugins, and anything that goes out over the air, needs the admin password of this MeshPulse.</p>' +
            '<input type="password" name="password" autocomplete="current-password" aria-label="Admin password" required>' +
            '<div class="mp-err" role="alert"></div>' +
            '<div class="mp-row"><button type="button" class="mp-cancel">Cancel</button>' +
            '<button type="submit" class="mp-go">Unlock</button></div></form>';
        document.body.appendChild(dlg);
        var form = dlg.querySelector('form'), input = dlg.querySelector('input');
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            if (!input.value) return;
            dlg.querySelector('.mp-go').disabled = true;
            dlg.querySelector('.mp-err').textContent = '';
            S.raw(JSON.stringify({type: 'auth', password: input.value}));
        });
        dlg.querySelector('.mp-cancel').addEventListener('click', function () {
            S.queue = [];
            closePrompt();
        });
        setTimeout(function () { input.focus(); }, 0);
    }

    function closePrompt() {
        if (dlg) dlg.remove();
        dlg = null;
    }

    function showError(d) {
        if (!dlg) { prompt(); }
        if (!dlg) return;
        var msg = d.error === 'locked'
            ? 'Too many attempts. Try again in ' + (d.retry_after || 60) + ' s.'
            : 'Wrong password.';
        dlg.querySelector('.mp-err').textContent = msg;
        dlg.querySelector('.mp-go').disabled = false;
        var input = dlg.querySelector('input');
        input.select();
    }

    // ── wiring ──────────────────────────────────────────────────────────

    function onMessage(ev) {
        var d;
        try { d = JSON.parse(ev.data); } catch (e) { return; }
        if (!d || !d.type) return;
        if (d.type === 'auth_status') {
            S.known = true;
            S.required = !!d.required;
            S.authed = false;
            S.protected = {};
            (d.protected || []).forEach(function (t) { S.protected[t] = true; });
            var tok = getToken();
            if (S.required && tok) {
                S.tokenPending = true;
                S.raw(JSON.stringify({type: 'auth', token: tok}));
            } else if (!S.required) {
                closePrompt();
                flush();
            }
            notify();
        } else if (d.type === 'auth_result') {
            S.tokenPending = false;
            if (d.ok) {
                S.authed = true;
                if (d.token) setToken(d.token);
                closePrompt();
                flush();
            } else if (d.error === 'expired') {
                setToken(null);
                if (S.queue.length) prompt();
            } else {
                showError(d);
            }
            notify();
        } else if (d.type === 'auth_required') {
            // Sent before we knew a password was set, or a session ended: ask, then resend.
            S.required = true;
            S.authed = false;
            var m = S.last[d.request_type];
            if (m && S.queue.indexOf(m) < 0) S.queue.push(m);
            prompt();
            notify();
        } else if (d.type === 'admin_password_result') {
            if (d.ok) {
                S.required = !!d.required;
                if (d.token) { setToken(d.token); S.authed = true; }
                if (!d.required) { setToken(null); S.authed = false; }
            }
            notify();
        } else if (d.type === 'admin_sign_out_result' && d.ok) {
            setToken(null);
            S.authed = false;
            notify();
        }
    }

    function attach(ws) {
        if (!ws || ws._meshAuth) return ws;
        ws._meshAuth = true;
        S.ws = ws;
        S.known = false;
        S.authed = false;
        S.tokenPending = false;
        var raw = ws.send.bind(ws);
        S.raw = raw;
        ws.send = function (msg) {
            var t = typeOf(msg);
            // Remember every type, not just the ones known to be protected: a page
            // sends its first requests before the backend has said which those are,
            // and a refused one must be resent after the login.
            if (t) S.last[t] = msg;
            if (t && S.required && !S.authed && S.protected[t]) {
                if (S.queue.indexOf(msg) < 0) S.queue.push(msg);
                prompt();
                return;
            }
            raw(msg);
        };
        ws.addEventListener('message', onMessage);
        return ws;
    }

    // Requests that change the password go through the same queue as the rest:
    // changing or removing a set password needs an unlocked browser.
    function send(obj) {
        var msg = JSON.stringify(obj);
        if (S.required && !S.authed) { S.queue.push(msg); prompt(); return; }
        rawSend(msg);
    }

    window.MeshAuth = {
        attach: attach,
        state: state,
        onChange: function (cb) { S.listeners.push(cb); cb(state()); },
        setPassword: function (pw) { send({type: 'set_admin_password', password: pw}); },
        removePassword: function () { send({type: 'set_admin_password', password: ''}); },
        signOutEverywhere: function () { send({type: 'admin_sign_out_everywhere'}); },
        // This browser only: forget the token, keep the password.
        signOut: function () { setToken(null); S.authed = false; notify(); if (S.ws) S.ws.close(); }
    };
})();
