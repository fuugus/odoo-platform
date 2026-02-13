/* ═══════════════════════════════════════════════════════════════════════════
   Odoo Deployment Platform - Admin Panel JavaScript
   ═══════════════════════════════════════════════════════════════════════════ */

// ─── Toast Notifications ────────────────────────────────────────────────────

function showToast(message, type = 'info') {
    let container = document.querySelector('.toast-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.remove();
        if (container.children.length === 0) {
            container.remove();
        }
    }, 3000);
}

// ─── Auto-scroll log containers ─────────────────────────────────────────────

function autoScrollLog(element) {
    if (element) {
        element.scrollTop = element.scrollHeight;
    }
}

// ─── Keyboard shortcut: Ctrl+K for quick navigation ────────────────────────

document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'k') {
        e.preventDefault();
        const pages = [
            { name: 'Setup Wizard', url: '/setup' },
            { name: 'Dashboard', url: '/dashboard' },
            { name: 'Instances', url: '/instances' },
            { name: 'Deploy', url: '/deploy' },
            { name: 'Databases', url: '/databases' },
        ];
        const choice = prompt(
            'Navigation:\n' +
            pages.map((p, i) => `${i + 1}. ${p.name}`).join('\n') +
            '\n\nEnter number:'
        );
        const idx = parseInt(choice) - 1;
        if (idx >= 0 && idx < pages.length) {
            window.location.href = pages[idx].url;
        }
    }
});

// ─── Instance Status Poller ─────────────────────────────────────────────────

const StatusPoller = (() => {
    const FAST_INTERVAL = 250;
    const SLOW_INTERVAL = 5000;
    const FAST_TIMEOUT = 15000;

    let pending = {};
    let timer = null;
    let updateCallback = null;

    function hasPending() {
        return Object.keys(pending).length > 0;
    }

    function schedule() {
        if (timer) clearTimeout(timer);
        timer = setTimeout(poll, hasPending() ? FAST_INTERVAL : SLOW_INTERVAL);
    }

    async function poll() {
        try {
            const resp = await fetch('/api/instances');
            if (!resp.ok) { schedule(); return; }
            const data = await resp.json();

            const now = Date.now();
            for (const [name, info] of Object.entries(pending)) {
                const inst = data[name];
                if (!inst) { delete pending[name]; continue; }
                const settled = (info.state === 'restarting' && inst.status === 'active')
                             || (info.state === 'stopping' && inst.status !== 'active')
                             || (now - info.since > FAST_TIMEOUT);
                if (settled) delete pending[name];
            }

            if (updateCallback) updateCallback(data, pending);
        } catch (e) {}
        schedule();
    }

    return {
        init(callback) {
            updateCallback = callback;
            schedule();
        },
        setPending(name, state) {
            pending[name] = { state, since: Date.now() };
            schedule();
        },
        getPending(name) {
            return pending[name] || null;
        },
        pollNow() {
            if (timer) clearTimeout(timer);
            poll();
        }
    };
})();

// ─── WebSocket Helper ──────────────────────────────────────────────────────

function connectWebSocket(path, logElement, callbacks = {}) {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${location.host}${path}`);
    const pre = logElement.querySelector('pre') || logElement;
    let lastError = '';

    logElement.style.display = 'block';
    pre.textContent = '';

    ws.onopen = () => {
        if (callbacks.onOpen) callbacks.onOpen(ws);
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'log') {
            pre.textContent += data.message + '\n';
            autoScrollLog(logElement);
        } else if (data.type === 'status') {
            if (callbacks.onStatus) callbacks.onStatus(data.status, ws, lastError);
        } else if (data.type === 'error') {
            lastError = data.message;
            pre.textContent += `ERROR: ${data.message}\n`;
            if (callbacks.onError) callbacks.onError(data.message, ws);
        }
    };

    ws.onerror = () => {
        pre.textContent += 'WebSocket connection error\n';
        if (callbacks.onWsError) callbacks.onWsError(ws);
    };

    ws.onclose = () => {
        if (callbacks.onClose) callbacks.onClose(ws, lastError);
    };

    return ws;
}

// ─── Connection status indicator ────────────────────────────────────────────

async function checkHealth() {
    try {
        const resp = await fetch('/api/health');
        if (resp.ok) {
            return true;
        }
    } catch (e) {}
    return false;
}

setInterval(async () => {
    const healthy = await checkHealth();
    const footer = document.querySelector('.sidebar-footer a');
    if (footer) {
        footer.textContent = healthy ? '● API Online' : '○ API Offline';
        footer.style.color = healthy ? '#2ecc71' : '#e74c3c';
    }
}, 15000);
