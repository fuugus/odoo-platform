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
        // Simple quick nav
        const pages = [
            { name: 'Setup Wizard', url: '/setup' },
            { name: 'Dashboard', url: '/dashboard' },
            { name: 'Instanzen', url: '/instances' },
            { name: 'Deploy', url: '/deploy' },
            { name: 'Datenbanken', url: '/databases' },
        ];
        const choice = prompt(
            'Navigation:\n' +
            pages.map((p, i) => `${i + 1}. ${p.name}`).join('\n') +
            '\n\nNummer eingeben:'
        );
        const idx = parseInt(choice) - 1;
        if (idx >= 0 && idx < pages.length) {
            window.location.href = pages[idx].url;
        }
    }
});

// ─── Connection status indicator ────────────────────────────────────────────

async function checkHealth() {
    try {
        const resp = await fetch('/api/health');
        if (resp.ok) {
            return true;
        }
    } catch (e) {
        // Server unreachable
    }
    return false;
}

// Check health periodically
setInterval(async () => {
    const healthy = await checkHealth();
    const footer = document.querySelector('.sidebar-footer a');
    if (footer) {
        footer.textContent = healthy ? '● API Online' : '○ API Offline';
        footer.style.color = healthy ? '#2ecc71' : '#e74c3c';
    }
}, 15000);
