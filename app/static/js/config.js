/**
 * UI and Logic for the Configuration page
 */

let fiorilliStatus = 'ok';
let ahgoraStatus = 'ok';
let initialValues = {};

function showTab(tabId, btn) {
    // Toggle tabs visibility
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    const targetTab = document.getElementById('tab-' + tabId);
    if (targetTab) {
        targetTab.classList.add('active');
    }

    // Toggle active button style
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    if (btn) {
        btn.classList.add('active');
    }

    // Update URL hash without scroll
    history.replaceState(null, null, ' ' + window.location.pathname + '#' + tabId);
}

function updateSaveButtonState() {
    const saveBtn = document.querySelector('#configForm button[type="submit"]');
    if (!saveBtn) return;
    
    const canSave = fiorilliStatus === 'ok' && ahgoraStatus === 'ok';
    saveBtn.disabled = !canSave;
    if (canSave) {
        saveBtn.classList.remove('opacity-50', 'cursor-not-allowed');
    } else {
        saveBtn.classList.add('opacity-50', 'cursor-not-allowed');
    }
}

async function testCredentials(system) {
    const btn = document.getElementById(`test-${system}-btn`);
    const spinner = document.getElementById(`${system}-btn-spinner`);
    const resultSpan = document.getElementById(`${system}-test-result`);
    
    if (!btn || !spinner || !resultSpan) return;
    
    // Set loading state
    btn.disabled = true;
    spinner.classList.remove('hidden');
    resultSpan.textContent = 'Testando...';
    resultSpan.className = 'text-sm font-medium text-slate-500 animate-pulse';
    
    const form = document.getElementById('configForm');
    const formData = new FormData(form);
    
    try {
        const response = await fetch(`/api/user/credentials/test/${system}`, {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();
        
        if (data.status === 'ok') {
            resultSpan.textContent = data.message || 'Conexão OK!';
            resultSpan.className = 'text-sm font-medium text-emerald-600';
            if (system === 'fiorilli') fiorilliStatus = 'ok';
            if (system === 'ahgora') ahgoraStatus = 'ok';
        } else {
            resultSpan.textContent = data.message || 'Falha na conexão.';
            resultSpan.className = 'text-sm font-medium text-rose-600';
            if (system === 'fiorilli') fiorilliStatus = 'failed';
            if (system === 'ahgora') ahgoraStatus = 'failed';
        }
    } catch (error) {
        resultSpan.textContent = 'Erro ao realizar o teste.';
        resultSpan.className = 'text-sm font-medium text-rose-600';
        if (system === 'fiorilli') fiorilliStatus = 'failed';
        if (system === 'ahgora') ahgoraStatus = 'failed';
    } finally {
        btn.disabled = false;
        spinner.classList.add('hidden');
        updateSaveButtonState();
    }
}

function setupInputListeners() {
    const fiorilliInputs = [
        document.getElementById('fiorilli-url'),
        document.getElementById('fiorilli-user'),
        document.getElementById('fiorilli-password')
    ];
    
    const ahgoraInputs = [
        document.getElementById('ahgora-url'),
        document.getElementById('ahgora-company'),
        document.getElementById('ahgora-user'),
        document.getElementById('ahgora-password')
    ];
    
    // Store initial values
    fiorilliInputs.forEach(el => {
        if (el) initialValues[el.id] = el.value || '';
    });
    ahgoraInputs.forEach(el => {
        if (el) initialValues[el.id] = el.value || '';
    });
    
    // Helper to check if a system is completely empty
    const isSystemEmpty = (inputs) => {
        return inputs.every(el => !el || !el.value.trim());
    };
    
    // Helper to check if any field has changed from its initial value
    const hasSystemChanged = (inputs) => {
        return inputs.some(el => el && (el.value || '') !== (initialValues[el.id] || ''));
    };
    
    const handleFiorilliChange = () => {
        const resultSpan = document.getElementById('fiorilli-test-result');
        if (isSystemEmpty(fiorilliInputs)) {
            fiorilliStatus = 'ok'; // empty is valid
            if (resultSpan) {
                resultSpan.textContent = '';
            }
        } else if (hasSystemChanged(fiorilliInputs)) {
            fiorilliStatus = 'untested';
            if (resultSpan) {
                resultSpan.textContent = 'Campos alterados. Realize o teste para poder salvar.';
                resultSpan.className = 'text-sm font-medium text-amber-600';
            }
        } else {
            fiorilliStatus = 'ok'; // matches initial values
            if (resultSpan) {
                resultSpan.textContent = '';
            }
        }
        updateSaveButtonState();
    };
    
    const handleAhgoraChange = () => {
        const resultSpan = document.getElementById('ahgora-test-result');
        if (isSystemEmpty(ahgoraInputs)) {
            ahgoraStatus = 'ok'; // empty is valid
            if (resultSpan) {
                resultSpan.textContent = '';
            }
        } else if (hasSystemChanged(ahgoraInputs)) {
            ahgoraStatus = 'untested';
            if (resultSpan) {
                resultSpan.textContent = 'Campos alterados. Realize o teste para poder salvar.';
                resultSpan.className = 'text-sm font-medium text-amber-600';
            }
        } else {
            ahgoraStatus = 'ok'; // matches initial values
            if (resultSpan) {
                resultSpan.textContent = '';
            }
        }
        updateSaveButtonState();
    };
    
    fiorilliInputs.forEach(el => {
        if (el) el.addEventListener('input', handleFiorilliChange);
    });
    ahgoraInputs.forEach(el => {
        if (el) el.addEventListener('input', handleAhgoraChange);
    });
    
    // Check initial state
    handleFiorilliChange();
    handleAhgoraChange();
}

/**
 * Initializes the configuration page state
 * @param {Object} options - State options from the server
 */
async function initConfig(options = {}) {
    const hash = window.location.hash.substring(1);

    // Auto-select tab based on hash or errors
    if (options.hasPasswordError || options.hasPasswordSuccess || window.location.pathname === '/change-password') {
        const btn = document.querySelector('a[href="#password"]');
        if (btn) showTab('password', btn);
    } else if (options.hasCreateUserError || options.hasCreateUserSuccess || window.location.pathname === '/create-user') {
        const btn = document.querySelector('a[href="#create-user"]');
        if (btn) showTab('create-user', btn);
    } else if (hash) {
        const btn = document.querySelector(`a[href="#${hash}"]`);
        if (btn) showTab(hash, btn);
    }
    
    // Set up listeners for validation
    setupInputListeners();
}

// Export test function to global window scope so it can be called from onclick
window.testCredentials = testCredentials;
