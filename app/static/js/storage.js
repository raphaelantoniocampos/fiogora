// Helper functions for encryption
function str2ab(str) {
    const buf = new ArrayBuffer(str.length);
    const bufView = new Uint8Array(buf);
    for (let i = 0, strLen = str.length; i < strLen; i++) {
        bufView[i] = str.charCodeAt(i);
    }
    return buf;
}

async function importPublicKey(pemString) {
    const pemHeader = "-----BEGIN PUBLIC KEY-----";
    const pemFooter = "-----END PUBLIC KEY-----";
    let pemContent = pemString.substring(pemString.indexOf(pemHeader) + pemHeader.length, pemString.indexOf(pemFooter));
    pemContent = pemContent.replace(/\s/g, '');

    const binaryDerString = window.atob(pemContent);
    const binaryDer = str2ab(binaryDerString);

    try {
        return await window.crypto.subtle.importKey(
            "spki",
            binaryDer,
            {
                name: "RSA-OAEP",
                hash: "SHA-256"
            },
            true,
            ["encrypt"]
        );
    }
    catch (TypeError) {
        return false;
    }
}

async function encryptString(text, publicKey) {
    const enc = new TextEncoder();
    const encoded = enc.encode(text);
    const encrypted = await window.crypto.subtle.encrypt(
        {
            name: "RSA-OAEP"
        },
        publicKey,
        encoded
    );

    let binary = '';
    const bytes = new Uint8Array(encrypted);
    const len = bytes.byteLength;
    for (let i = 0; i < len; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return window.btoa(binary);
}

// Action functions
async function startSync(event) {
    const btn = event.currentTarget;
    const btnText = document.getElementById("syncBtnText");

    btn.disabled = true;
    if (btnText) btnText.innerText = "Iniciando...";

    try {
        const response = await fetch('/api/sync/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Falha ao iniciar sincronização");
        }

        setTimeout(() => typeof htmx !== 'undefined' && htmx.trigger('#jobs-list', 'refresh'), 500);
    } catch (error) {
        if (error.message == "User credentials not found") {
            console.log(error);
            alert("Configure as credenciais dos sistemas")
            window.location.href = "/config#automation"
        }
        else {
            alert(error.message || "Ocorreu um erro ao iniciar a sincronização.");
            console.error(error);
        }
    } finally {
        btn.disabled = false;
        if (btnText) btnText.innerText = "Iniciar Sincronização";
    }
}

async function executeBatch(jobId, taskType, btn) {
    if (btn) {
        btn.disabled = true;
        btn.classList.add('opacity-50', 'cursor-not-allowed');
    }

    try {
        const url = `/api/sync/tasks/batch/execute?job_id=${jobId}&task_type=${encodeURIComponent(taskType)}`;

        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Falha ao executar lote");
        }

        if (typeof htmx !== 'undefined') {
            htmx.trigger(document.body, 'refresh');
            if (btn) {
                btn.disabled = false;
                btn.classList.remove('opacity-50', 'cursor-not-allowed');
            }
        } else {
            window.location.reload();
        }
    } catch (error) {
        alert(error.message || "Erro ao executar lote.");
        console.error(error);
        if (btn) {
            btn.disabled = false;
            btn.classList.remove('opacity-50', 'cursor-not-allowed');
        }
    }
}

async function executeTask(taskId, btn) {
    if (btn) {
        btn.disabled = true;
        btn.classList.add('opacity-50', 'cursor-not-allowed');
    }

    try {
        const response = await fetch(`/api/sync/tasks/${taskId}/execute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Falha ao executar tarefa");
        }

        if (typeof htmx !== 'undefined') {
            htmx.trigger(document.body, 'refresh');
            if (btn) {
                btn.disabled = false;
                btn.classList.remove('opacity-50', 'cursor-not-allowed');
            }
        } else {
            window.location.reload();
        }
    } catch (error) {
        alert(error.message || "Erro ao executar tarefa.");
        console.error(error);
        if (btn) {
            btn.disabled = false;
            btn.classList.remove('opacity-50', 'cursor-not-allowed');
        }
    }
}

async function cancelBatch(jobId, taskType, btn) {
    if (btn) {
        btn.disabled = true;
        btn.classList.add('opacity-50', 'cursor-not-allowed');
    }

    try {
        const response = await fetch(`/api/sync/tasks/batch/cancel?job_id=${jobId}&task_type=${encodeURIComponent(taskType)}`, {
            method: 'POST'
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Falha ao cancelar lote");
        }

        if (typeof htmx !== 'undefined') {
            htmx.trigger(document.body, 'refresh');
            if (btn) {
                btn.disabled = false;
                btn.classList.remove('opacity-50', 'cursor-not-allowed');
            }
        } else {
            window.location.reload();
        }
    } catch (error) {
        alert(error.message || "Erro ao cancelar lote.");
        console.error(error);
        if (btn) {
            btn.disabled = false;
            btn.classList.remove('opacity-50', 'cursor-not-allowed');
        }
    }
}

// Global exposure
window.startSync = startSync;
window.executeBatch = executeBatch;
window.executeTask = executeTask;
window.cancelBatch = cancelBatch;
